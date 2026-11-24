#!/usr/bin/env python3
"""MonitorLicita — monitor de licitações em andamento por UASG.

Servidor HTTP sem dependências externas (somente biblioteca padrão).
Fontes:
  * API de Dados Abertos do Compras.gov.br (lista de contratações Lei 14.133)
  * API do PNCP (itens, anexos, fontes orçamentárias, histórico, links)

Uso:  python3 server.py [--port 8000]
"""
import argparse
import json
import os
import re
import sqlite3
import ssl
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("MONITOR_DB", os.path.join(BASE_DIR, "monitor.db"))
STATIC_DIR = os.path.join(BASE_DIR, "static")

DADOS_ABERTOS = "https://dadosabertos.compras.gov.br"
PNCP = "https://pncp.gov.br/api"

# codigoModalidade aceito pela API de dados abertos (1..13). As que retornarem
# vazio custam só uma chamada.
MODALIDADES = list(range(1, 14))
JANELA_DIAS_PADRAO = 365
DETALHE_TTL = 6 * 3600

# Sugestões de órgão demandante para o SEI de Rondônia (prefixo do nº do processo).
# São apenas sugestões iniciais, inferidas das siglas que aparecem nos objetos;
# podem ser editadas na tela de configuração.
SUGESTOES_SEI_RO = {
    "0002": "AGEVISA", "0004": "CBMRO", "0009": "DER", "0015": "IDARON",
    "0020": "PGE", "0021": "PMRO", "0026": "SEAS", "0029": "SEDUC",
    "0030": "SEFIN", "0031": "SEGEP", "0033": "SEJUS", "0036": "SESAU",
    "0037": "SESDEC", "0043": "SUPEL", "0052": "FHEMERON", "0065": "FEASE",
}

SIGLA_RE = re.compile(r"\b([A-Z][A-Z]{2,9})(?:/RO)?\b")
SIGLAS_IGNORADAS = {
    "SRP", "CNPJ", "KIT", "NAO", "RAIO", "APARELHO", "VINCULADO", "CORPO",
    "BOMBEIROS", "QUARTEL", "LTDA", "EPP", "ME", "TR", "ETP", "RO", "IATA",
    "VPN", "BIT", "WAF", "STFC", "DIURNA", "NOTURNA", "ESCALAS", "MIRIM",
    "RENAME", "ANAC", "PTSA", "PCCR", "SUV", "GRS", "SIP", "DEAM", "GERES",
    "PARA", "COM", "DOS", "DAS", "SEM", "TIPO", "LOTE", "ITEM", "ITENS",
}

_ssl_ctx = ssl.create_default_context()
_ca = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
if _ca and os.path.exists(_ca):
    _ssl_ctx.load_verify_locations(_ca)

_lock = threading.Lock()


# --------------------------------------------------------------------------- DB
def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS uasg (
                codigo TEXT PRIMARY KEY,
                nome TEXT,
                limite_padrao REAL NOT NULL DEFAULT 0,
                janela_dias INTEGER NOT NULL DEFAULT 365,
                ultima_atualizacao TEXT,
                primeira_carga TEXT
            );
            CREATE TABLE IF NOT EXISTS regra (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uasg TEXT NOT NULL,
                tipo TEXT NOT NULL CHECK (tipo IN ('processo','texto')),
                padrao TEXT NOT NULL,
                orgao TEXT NOT NULL,
                limite REAL,
                ignorar INTEGER NOT NULL DEFAULT 0,
                UNIQUE (uasg, tipo, padrao)
            );
            CREATE TABLE IF NOT EXISTS compra (
                id_compra TEXT PRIMARY KEY,
                uasg TEXT NOT NULL,
                dados TEXT NOT NULL,
                primeira_vez TEXT NOT NULL,
                atualizado TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS detalhe (
                id_compra TEXT PRIMARY KEY,
                dados TEXT NOT NULL,
                obtido REAL NOT NULL
            );
            """
        )
        # migrações (bancos criados por versões anteriores)
        cols_u = {r[1] for r in con.execute("PRAGMA table_info(uasg)")}
        for col, ddl in (
            ("primeira_carga", "TEXT"),
            ("modo_alcada", "TEXT NOT NULL DEFAULT 'porte_abc'"),
            ("corte_abc", "REAL NOT NULL DEFAULT 80"),
            ("garantia", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if col not in cols_u:
                con.execute(f"ALTER TABLE uasg ADD COLUMN {col} {ddl}")
        if "porte" not in {r[1] for r in con.execute("PRAGMA table_info(regra)")}:
            con.execute("ALTER TABLE regra ADD COLUMN porte TEXT")


# ------------------------------------------------------------------------- HTTP
def http_json(url, params=None, timeout=90, tentativas=3):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    ultimo = None
    for i in range(tentativas):
        try:
            req = urllib.request.Request(
                url, headers={"Accept": "application/json", "User-Agent": "MonitorLicita/1.0"}
            )
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx) as r:
                corpo = r.read()
                if not corpo:
                    return None
                return json.loads(corpo)
        except urllib.error.HTTPError as e:
            if e.code in (204, 404):
                return None
            ultimo = e
        except Exception as e:  # noqa: BLE001
            ultimo = e
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"Falha ao consultar {url}: {ultimo}")


# -------------------------------------------------------------- Coleta (lista)
def buscar_contratacoes(uasg, janela_dias):
    fim = date.today()
    ini = fim - timedelta(days=janela_dias)

    def por_modalidade(mod):
        out, pagina = [], 1
        while True:
            d = http_json(
                f"{DADOS_ABERTOS}/modulo-contratacoes/1_consultarContratacoes_PNCP_14133",
                {
                    "pagina": pagina,
                    "tamanhoPagina": 500,
                    "unidadeOrgaoCodigoUnidade": uasg,
                    "dataPublicacaoPncpInicial": ini.isoformat(),
                    "dataPublicacaoPncpFinal": fim.isoformat(),
                    "codigoModalidade": mod,
                },
            )
            if not d:
                break
            out += d.get("resultado") or []
            if pagina >= (d.get("totalPaginas") or 1):
                break
            pagina += 1
        return out

    with ThreadPoolExecutor(max_workers=4) as ex:
        resultados = list(ex.map(por_modalidade, MODALIDADES))
    return [r for lote in resultados for r in lote]


def atualizar_uasg(uasg):
    with db() as con:
        row = con.execute("SELECT * FROM uasg WHERE codigo=?", (uasg,)).fetchone()
    if not row:
        raise ValueError("UASG não cadastrada")
    regs = buscar_contratacoes(uasg, row["janela_dias"])
    agora = datetime.now().isoformat(timespec="seconds")
    nome = row["nome"]
    with _lock, db() as con:
        for r in regs:
            if r.get("contratacaoExcluida"):
                continue
            nome = nome or r.get("unidadeOrgaoNomeUnidade")
            con.execute(
                """INSERT INTO compra (id_compra, uasg, dados, primeira_vez, atualizado)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(id_compra) DO UPDATE SET dados=excluded.dados,
                                                       atualizado=excluded.atualizado""",
                (r["idCompra"], uasg, json.dumps(r, ensure_ascii=False), agora, agora),
            )
        con.execute(
            "UPDATE uasg SET ultima_atualizacao=?, nome=?, primeira_carga=COALESCE(primeira_carga, ?) WHERE codigo=?",
            (agora, nome, agora, uasg),
        )
    return len(regs)


# ------------------------------------------------------------- Classificação
def prefixo_processo(processo):
    """Primeiro bloco numérico do nº do processo (no SEI-RO identifica o órgão)."""
    if not processo:
        return None
    m = re.match(r"\s*(\d{4})", processo)
    return m.group(1) if m else None


def status_andamento(r, agora):
    sit = (r.get("situacaoCompraNomePncp") or "").lower()
    if "suspens" in sit:
        return "suspensa"
    if "revogad" in sit or "anulad" in sit:
        return "encerrada"
    if r.get("existeResultado") or r.get("valorTotalHomologado"):
        return "com_resultado"
    ab = r.get("dataAberturaPropostaPncp")
    enc = r.get("dataEncerramentoPropostaPncp")
    if ab and agora < ab:
        return "aguardando_abertura"
    if enc and agora < enc:
        return "recebendo_propostas"
    return "em_julgamento"


ANDAMENTO = {"aguardando_abertura", "recebendo_propostas", "em_julgamento", "suspensa"}


def carregar_regras(con):
    regras = {}
    for r in con.execute("SELECT * FROM regra ORDER BY tipo, length(padrao) DESC"):
        regras.setdefault(r["uasg"], []).append(dict(r))
    return regras


def resolver_orgao(r, uasg_row, regras):
    pref = prefixo_processo(r.get("processo"))
    texto = ((r.get("objetoCompra") or "") + " " + (r.get("informacaoComplementar") or "")).upper()
    for rg in regras:
        if rg["tipo"] == "processo" and pref and pref == rg["padrao"]:
            return rg, pref
    for rg in regras:
        if rg["tipo"] == "texto" and rg["padrao"].upper() in texto:
            return rg, pref
    return None, pref


def listar(params):
    agora = datetime.now().isoformat(timespec="seconds")
    so_andamento = params.get("andamento", "1") != "0"
    aplicar_limite = params.get("limite", "1") != "0"
    with db() as con:
        uasgs = {u["codigo"]: dict(u) for u in con.execute("SELECT * FROM uasg")}
        regras = carregar_regras(con)
        rows = con.execute("SELECT * FROM compra").fetchall()
    compras_por_uasg = {}
    for row in rows:
        compras_por_uasg.setdefault(row["uasg"], []).append(json.loads(row["dados"]))
    alcadas = {
        c: calcular_alcada(u, compras_por_uasg.get(c, []), regras.get(c, []))
        for c, u in uasgs.items() if u.get("modo_alcada") == "porte_abc"
    }
    out = []
    for row in rows:
        u = uasgs.get(row["uasg"])
        if not u:
            continue
        r = json.loads(row["dados"])
        st = status_andamento(r, agora)
        if so_andamento and st not in ANDAMENTO:
            continue
        regra, pref = resolver_orgao(r, u, regras.get(row["uasg"], []))
        if regra and regra["ignorar"]:
            continue
        alc = alcadas.get(row["uasg"])
        porte = alc["porte"].get(pref or "?") if alc else None
        if regra and regra["limite"] is not None:
            limite, origem = regra["limite"], "manual do órgão"
        elif alc and (pref or "?") in alc["limite"]:
            limite = alc["limite"][pref or "?"]
            origem = (f"garantia: maior compra do órgão" if alc["garantido"][pref or "?"]
                      else f"régua porte {NOME_PORTE[porte]} · ABC {u['corte_abc']:.0f}%")
        else:
            limite, origem = u["limite_padrao"], "padrão da UASG"
        valor = r.get("valorTotalEstimado") or 0
        sigiloso = (r.get("orcamentoSigilosoCodigo") or 1) != 1 or valor <= 1
        abaixo = (not sigiloso) and valor < (limite or 0)
        if aplicar_limite and abaixo:
            continue
        sugestao = SUGESTOES_SEI_RO.get(pref) if (r.get("unidadeOrgaoUfSigla") == "RO") else None
        out.append(
            {
                "id": r["idCompra"],
                "uasg": row["uasg"],
                "uasgNome": u["nome"],
                "orgao": regra["orgao"] if regra else (sugestao and f"{sugestao}?") or (f"Proc. {pref}" if pref else "—"),
                "orgaoDefinido": bool(regra),
                "prefixo": pref,
                "limiteAplicado": limite,
                "origemLimite": origem,
                "porte": porte,
                "status": st,
                "sigiloso": sigiloso,
                # "novo" = publicada no PNCP nos últimos 7 dias
                "novo": (r.get("dataPublicacaoPncp") or "") >= (datetime.now() - timedelta(days=7)).isoformat(),
                "primeiraVez": row["primeira_vez"],
                "numero": f'{r.get("numeroCompra")}/{r.get("anoCompraPncp")}',
                "modalidade": r.get("modalidadeNome"),
                "modoDisputa": r.get("modoDisputaNomePncp"),
                "srp": r.get("srp"),
                "amparo": r.get("amparoLegalNome"),
                "amparoDescricao": r.get("amparoLegalDescricao"),
                "processo": r.get("processo"),
                "objeto": r.get("objetoCompra"),
                "informacaoComplementar": r.get("informacaoComplementar"),
                "valorEstimado": valor,
                "valorHomologado": r.get("valorTotalHomologado"),
                "situacao": r.get("situacaoCompraNomePncp"),
                "instrumento": r.get("tipoInstrumentoConvocatorioNome"),
                "publicacao": r.get("dataPublicacaoPncp"),
                "atualizacao": r.get("dataAtualizacaoPncp"),
                "abertura": r.get("dataAberturaPropostaPncp"),
                "encerramento": r.get("dataEncerramentoPropostaPncp"),
                "municipio": r.get("unidadeOrgaoMunicipioNome"),
                "uf": r.get("unidadeOrgaoUfSigla"),
                "cnpj": r.get("orgaoEntidadeCnpj"),
                "ano": r.get("anoCompraPncp"),
                "sequencial": r.get("sequencialCompraPncp"),
                "numeroControlePNCP": r.get("numeroControlePNCP"),
                "linkPncp": f'https://pncp.gov.br/app/editais/{r.get("orgaoEntidadeCnpj")}/{r.get("anoCompraPncp")}/{r.get("sequencialCompraPncp")}',
                "linkCompras": f'https://cnetmobile.estaleiro.serpro.gov.br/comprasnet-web/public/compras/acompanhamento-compra?compra={r["idCompra"]}',
            }
        )
    out.sort(key=lambda x: (x["encerramento"] or "9999"))
    return out


PORTES = ("G", "M", "P")
NOME_PORTE = {"G": "Grande", "M": "Médio", "P": "Pequeno"}


def corte_abc(valores, frac):
    """Curva ABC: menor valor que ainda pertence à classe A, i.e. as maiores compras
    que, somadas, atingem `frac` do valor total."""
    v = sorted(valores, reverse=True)
    total, acc = sum(v), 0.0
    for x in v:
        acc += x
        if acc >= frac * total:
            return x
    return v[-1] if v else None


def valor_comparavel(r):
    """Valor estimado utilizável para estatística (exclui sigilosos/simbólicos)."""
    v = r.get("valorTotalEstimado") or 0
    if (r.get("orcamentoSigilosoCodigo") or 1) != 1 or v <= 1:
        return None
    return v


def calcular_alcada(uasg_row, compras, regras):
    """Régua automática por porte do órgão demandante.

    1. Porte: órgãos (prefixo do processo) ordenados pelo volume estimado na janela e
       divididos em tercis -> Grande / Médio / Pequeno. Pode ser sobrescrito por regra.
    2. Régua da classe: corte da curva ABC das compras de todos os órgãos da classe
       (as maiores compras que somam `corte_abc`% do valor da classe).
    3. Garantia: a régua de um órgão nunca passa da maior compra dele na janela,
       assim nenhum órgão fica sem nenhuma licitação analisada.
    4. Piso: o limite padrão da UASG é a materialidade mínima; nenhuma régua fica abaixo dele.
    """
    frac = (uasg_row.get("corte_abc") or 80) / 100
    por = {}
    for r in compras:
        v = valor_comparavel(r)
        if v is not None:
            por.setdefault(prefixo_processo(r.get("processo")) or "?", []).append(v)
    override = {rg["padrao"]: rg["porte"] for rg in regras if rg["tipo"] == "processo" and rg.get("porte")}
    ordem = sorted(por, key=lambda o: -sum(por[o]))
    n = len(ordem)
    porte = {}
    for i, o in enumerate(ordem):
        porte[o] = override.get(o) or ("G" if i < n / 3 else "M" if i < 2 * n / 3 else "P")
    valores_classe = {k: [] for k in PORTES}
    for o, v in por.items():
        valores_classe[porte[o]] += v
    regua = {k: corte_abc(v, frac) for k, v in valores_classe.items()}
    limite, garantido = {}, {}
    for o, v in por.items():
        rg = regua[porte[o]]
        if rg is None:
            continue
        piso = uasg_row.get("limite_padrao") or 0
        if uasg_row.get("garantia", 1) and max(v) < rg:
            limite[o], garantido[o] = max(piso, max(v)), True
        else:
            limite[o], garantido[o] = max(piso, rg), False
    total = sum(sum(v) for v in por.values()) or 1
    sel = [(o, x) for o, v in por.items() for x in v if o in limite and x >= limite[o]]
    resumo = {
        "compras": sum(len(v) for v in por.values()),
        "selecionadas": len(sel),
        "valorCoberto": sum(x for _, x in sel) / total,
        "orgaosSemNada": sum(1 for o in por if not any(x >= limite.get(o, float("inf")) for x in por[o])),
    }
    return {
        "porte": porte, "regua": regua, "limite": limite, "garantido": garantido, "volume": {o: sum(v) for o, v in por.items()},
        "amostra": {o: len(v) for o, v in por.items()}, "resumo": resumo,
        "orgaosPorClasse": {k: sum(1 for o in porte if porte[o] == k) for k in PORTES},
    }


def simular(uasg_row, compras, regras, cortes=(70, 80, 90, 95)):
    out = []
    for c in cortes:
        for g in (1, 0):
            a = calcular_alcada({**uasg_row, "corte_abc": c, "garantia": g}, compras, regras)
            andamento = 0
            agora = datetime.now().isoformat(timespec="seconds")
            for r in compras:
                v = valor_comparavel(r)
                o = prefixo_processo(r.get("processo")) or "?"
                if v is not None and o in a["limite"] and v >= a["limite"][o] and status_andamento(r, agora) in ANDAMENTO:
                    andamento += 1
            out.append({"corte": c, "garantia": bool(g), **a["resumo"], "emAndamento": andamento,
                        "regua": a["regua"]})
    return out


def orgaos_detectados(uasg):
    """Agrupa as compras da UASG por órgão demandante (prefixo do processo) com o
    porte, a régua automática e a simulação de cortes ABC."""
    agora = datetime.now().isoformat(timespec="seconds")
    with db() as con:
        u = con.execute("SELECT * FROM uasg WHERE codigo=?", (uasg,)).fetchone()
        rows = con.execute("SELECT dados FROM compra WHERE uasg=?", (uasg,)).fetchall()
        regras = [dict(r) for r in con.execute("SELECT * FROM regra WHERE uasg=?", (uasg,))]
    if not u:
        raise ValueError("UASG não cadastrada")
    u = dict(u)
    compras = [json.loads(r["dados"]) for r in rows]
    alc = calcular_alcada(u, compras, regras)
    grupos = {}
    for r in compras:
        pref = prefixo_processo(r.get("processo")) or "?"
        g = grupos.setdefault(pref, {"prefixo": pref, "qtd": 0, "andamento": 0, "andamentoAcima": 0,
                                     "valor": 0.0, "siglas": {}, "exemplo": None, "maior": 0.0})
        g["qtd"] += 1
        v = valor_comparavel(r) or 0
        g["valor"] += v
        g["maior"] = max(g["maior"], v)
        if status_andamento(r, agora) in ANDAMENTO:
            g["andamento"] += 1
            if v >= alc["limite"].get(pref, float("inf")):
                g["andamentoAcima"] += 1
        g["exemplo"] = g["exemplo"] or (r.get("objetoCompra") or "")[:160]
        for sg in SIGLA_RE.findall(r.get("objetoCompra") or ""):
            if sg not in SIGLAS_IGNORADAS:
                g["siglas"][sg] = g["siglas"].get(sg, 0) + 1
    regra_por_pref = {r["padrao"]: r for r in regras if r["tipo"] == "processo"}
    out = []
    for g in grupos.values():
        sig = sorted(g["siglas"].items(), key=lambda kv: -kv[1])
        g["siglas"] = [x for x, _ in sig[:4]]
        g["sugestao"] = SUGESTOES_SEI_RO.get(g["prefixo"]) or (g["siglas"][0] if g["siglas"] else "")
        g["regra"] = regra_por_pref.get(g["prefixo"])
        g["porte"] = alc["porte"].get(g["prefixo"])
        g["porteManual"] = bool(g["regra"] and g["regra"].get("porte"))
        g["limiteAuto"] = alc["limite"].get(g["prefixo"])
        g["garantido"] = alc["garantido"].get(g["prefixo"], False)
        out.append(g)
    out.sort(key=lambda g: -g["valor"])
    return {
        "grupos": out,
        "regras": regras,
        "uasg": u,
        "regua": alc["regua"],
        "orgaosPorClasse": alc["orgaosPorClasse"],
        "resumo": alc["resumo"],
        "simulacao": simular(u, compras, regras),
    }


# ----------------------------------------------------------------- Detalhe
def detalhe(id_compra, forcar=False):
    with db() as con:
        c = con.execute("SELECT dados FROM compra WHERE id_compra=?", (id_compra,)).fetchone()
        cache = con.execute("SELECT * FROM detalhe WHERE id_compra=?", (id_compra,)).fetchone()
    if not c:
        raise ValueError("Compra não encontrada")
    if cache and not forcar and time.time() - cache["obtido"] < DETALHE_TTL:
        return json.loads(cache["dados"])
    r = json.loads(c["dados"])
    cnpj, ano, seq = r["orgaoEntidadeCnpj"], r["anoCompraPncp"], r["sequencialCompraPncp"]
    base = f"{PNCP}/pncp/v1/orgaos/{cnpj}/compras/{ano}/{seq}"

    def itens():
        todos, pagina = [], 1
        while True:
            lote = http_json(f"{base}/itens", {"pagina": pagina, "tamanhoPagina": 500}) or []
            todos += lote
            if len(lote) < 500:
                return todos
            pagina += 1

    with ThreadPoolExecutor(max_workers=4) as ex:
        f_cab = ex.submit(http_json, f"{PNCP}/consulta/v1/orgaos/{cnpj}/compras/{ano}/{seq}")
        f_itens = ex.submit(itens)
        f_arq = ex.submit(http_json, f"{base}/arquivos")
        f_hist = ex.submit(http_json, f"{base}/historico")
        cab, its, arqs, hist = f_cab.result(), f_itens.result(), f_arq.result(), f_hist.result()

    cab = cab or {}
    its = its or []
    beneficios, criterios, tipos = {}, {}, {"M": 0, "S": 0}
    for it in its:
        b = it.get("tipoBeneficioNome") or "—"
        beneficios[b] = beneficios.get(b, 0) + 1
        c_ = it.get("criterioJulgamentoNome") or "—"
        criterios[c_] = criterios.get(c_, 0) + 1
        tipos[it.get("materialOuServico") or "M"] = tipos.get(it.get("materialOuServico") or "M", 0) + 1
    # Retificações relevantes do histórico (documentos e alterações da compra)
    eventos = []
    for h in hist or []:
        if h.get("categoriaLogManutencaoNome") == "Item de Contratação" and h.get("tipoLogManutencaoNome") == "Inclusão":
            continue
        eventos.append(
            {
                "data": h.get("logManutencaoDataInclusao"),
                "tipo": h.get("tipoLogManutencaoNome"),
                "categoria": h.get("categoriaLogManutencaoNome"),
                "documento": h.get("documentoTitulo"),
                "justificativa": h.get("justificativa"),
            }
        )
    eventos.sort(key=lambda e: e["data"] or "", reverse=True)
    out = {
        "linkProcessoEletronico": cab.get("linkProcessoEletronico"),
        "linkSistemaOrigem": cab.get("linkSistemaOrigem"),
        "fontesOrcamentarias": [f.get("nome") for f in cab.get("fontesOrcamentarias") or []],
        "emendaParlamentar": cab.get("emendaParlamentar"),
        "justificativaPresencial": cab.get("justificativaPresencial"),
        "modoDisputa": cab.get("modoDisputaNome"),
        "qtdItens": len(its),
        "beneficios": beneficios,
        "criterios": criterios,
        "tipos": tipos,
        "itens": [
            {
                "numero": it.get("numeroItem"),
                "descricao": it.get("descricao"),
                "tipo": it.get("materialOuServicoNome"),
                "quantidade": it.get("quantidade"),
                "unidade": it.get("unidadeMedida"),
                "valorUnitario": it.get("valorUnitarioEstimado"),
                "valorTotal": it.get("valorTotal"),
                "beneficio": it.get("tipoBeneficioNome"),
                "criterio": it.get("criterioJulgamentoNome"),
                "situacao": it.get("situacaoCompraItemNome"),
                "temResultado": it.get("temResultado"),
                "catalogo": it.get("catalogoCodigoItem"),
                "sigiloso": it.get("orcamentoSigiloso"),
            }
            for it in its
        ],
        "arquivos": [
            {
                "titulo": a.get("titulo"),
                "tipo": a.get("tipoDocumentoNome"),
                "data": a.get("dataPublicacaoPncp"),
                "url": a.get("url"),
                "ativo": a.get("statusAtivo"),
            }
            for a in (arqs or [])
        ],
        "eventos": eventos[:30],
    }
    with _lock, db() as con:
        con.execute(
            "INSERT OR REPLACE INTO detalhe (id_compra, dados, obtido) VALUES (?,?,?)",
            (id_compra, json.dumps(out, ensure_ascii=False), time.time()),
        )
    return out


# ------------------------------------------------------------------- Handler
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silencioso
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _route(self, metodo):
        url = urllib.parse.urlparse(self.path)
        p = url.path
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if metodo == "GET" and p in ("/", "/index.html"):
                with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if metodo == "GET" and p == "/api/uasgs":
                with db() as con:
                    return self._send(200, [dict(r) for r in con.execute("SELECT * FROM uasg ORDER BY codigo")])
            if metodo == "POST" and p == "/api/uasgs":
                b = self._body()
                codigo = re.sub(r"\D", "", str(b.get("codigo", "")))
                if not codigo:
                    return self._send(400, {"erro": "Código de UASG inválido"})
                with db() as con:
                    atual = con.execute("SELECT * FROM uasg WHERE codigo=?", (codigo,)).fetchone()
                    atual = dict(atual) if atual else {}

                    def campo(k, padrao):
                        return b[k] if b.get(k) not in (None, "") else atual.get(k, padrao)

                    modo = campo("modo_alcada", "porte_abc")
                    if modo not in ("porte_abc", "fixo"):
                        return self._send(400, {"erro": "modo_alcada inválido"})
                    con.execute(
                        """INSERT INTO uasg (codigo, nome, limite_padrao, janela_dias, modo_alcada, corte_abc, garantia)
                           VALUES (?,?,?,?,?,?,?)
                           ON CONFLICT(codigo) DO UPDATE SET limite_padrao=excluded.limite_padrao,
                               janela_dias=excluded.janela_dias, modo_alcada=excluded.modo_alcada,
                               corte_abc=excluded.corte_abc, garantia=excluded.garantia,
                               nome=COALESCE(excluded.nome, uasg.nome)""",
                        (codigo, b.get("nome") or None, float(campo("limite_padrao", 0)),
                         int(campo("janela_dias", JANELA_DIAS_PADRAO)), modo,
                         min(99.0, max(50.0, float(campo("corte_abc", 80)))),
                         1 if campo("garantia", 1) in (1, True, "1", "true") else 0),
                    )
                return self._send(200, {"ok": True, "codigo": codigo})
            m = re.fullmatch(r"/api/uasgs/(\d+)", p)
            if m and metodo == "DELETE":
                with db() as con:
                    con.execute("DELETE FROM uasg WHERE codigo=?", (m[1],))
                    con.execute("DELETE FROM regra WHERE uasg=?", (m[1],))
                    con.execute("DELETE FROM detalhe WHERE id_compra IN (SELECT id_compra FROM compra WHERE uasg=?)", (m[1],))
                    con.execute("DELETE FROM compra WHERE uasg=?", (m[1],))
                return self._send(200, {"ok": True})
            m = re.fullmatch(r"/api/uasgs/(\d+)/atualizar", p)
            if m and metodo == "POST":
                n = atualizar_uasg(m[1])
                return self._send(200, {"ok": True, "registros": n})
            m = re.fullmatch(r"/api/uasgs/(\d+)/orgaos", p)
            if m and metodo == "GET":
                return self._send(200, orgaos_detectados(m[1]))
            if metodo == "POST" and p == "/api/regras":
                b = self._body()
                lim = b.get("limite")
                lim = None if lim in (None, "") else float(lim)
                with db() as con:
                    con.execute(
                        """INSERT INTO regra (uasg, tipo, padrao, orgao, limite, ignorar, porte) VALUES (?,?,?,?,?,?,?)
                           ON CONFLICT(uasg, tipo, padrao) DO UPDATE SET orgao=excluded.orgao,
                               limite=excluded.limite, ignorar=excluded.ignorar, porte=excluded.porte""",
                        (b["uasg"], b.get("tipo", "processo"), str(b["padrao"]).strip(),
                         (b.get("orgao") or b["padrao"]).strip(), lim, 1 if b.get("ignorar") else 0,
                         b.get("porte") if b.get("porte") in PORTES else None),
                    )
                return self._send(200, {"ok": True})
            m = re.fullmatch(r"/api/regras/(\d+)", p)
            if m and metodo == "DELETE":
                with db() as con:
                    con.execute("DELETE FROM regra WHERE id=?", (m[1],))
                return self._send(200, {"ok": True})
            if metodo == "GET" and p == "/api/licitacoes":
                return self._send(200, listar(q))
            m = re.fullmatch(r"/api/licitacoes/(\w+)", p)
            if m and metodo == "GET":
                return self._send(200, detalhe(m[1], forcar=q.get("forcar") == "1"))
            return self._send(404, {"erro": "não encontrado"})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"erro": str(e)})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    init_db()
    print(f"MonitorLicita em http://{a.host}:{a.port}")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
