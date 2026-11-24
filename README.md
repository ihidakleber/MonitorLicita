# MonitorLicita

Monitor de licitações em andamento por UASG, com limite de alçada por UASG e por órgão demandante.

## Como rodar

Precisa só de Python 3.9 ou mais novo, sem dependências externas.

```bash
python3 server.py --port 8000
```

Abra http://127.0.0.1:8000, cadastre uma UASG (ex.: `925373`) com o limite padrão e clique em **Atualizar dados**.

## Fontes de dados

| Dado | Fonte |
|---|---|
| Lista de contratações (Lei 14.133) por UASG | `dadosabertos.compras.gov.br/modulo-contratacoes/1_consultarContratacoes_PNCP_14133` |
| Itens, anexos, fontes de recurso, histórico, link do SEI | API do PNCP (`pncp.gov.br/api/...`), carregados ao abrir uma licitação e guardados em cache por 6 h |

## Status "em andamento"

- **Aguardando abertura**: antes da data de abertura das propostas
- **Recebendo propostas**: entre a abertura e o encerramento
- **Em disputa / julgamento**: propostas encerradas e ainda sem resultado
- **Suspensa**: situação "Suspensa" no PNCP

Ficam de fora as compras revogadas, anuladas e as que já têm resultado.

## Alçada por órgão demandante

Centrais de compras como a SUPEL/RO (UASG 925373) licitam para vários órgãos. O órgão
demandante é identificado pelo **prefixo do nº do processo** (no SEI-RO, `0036.xxxxx/2026` = SESAU).
Para cada prefixo dá para definir nome, limite próprio ou "ignorar". Também existem regras por
palavra no objeto, para os casos em que o processo não identifica o órgão.
A ordem de prioridade é: regra por prefixo, depois regra por palavra, depois o limite padrão da UASG.
Compras com valor sigiloso nunca são escondidas pelo limite.

### Sugestão de limite (P80)

Para cada prefixo, o sistema sugere como limite o **percentil 80** dos valores estimados das compras daquele
órgão dentro da janela carregada (sigilosos ficam de fora). Com esse limite, só aparecem os ~20% maiores
certames de cada órgão. Se o órgão tiver menos de 5 compras com valor conhecido, a sugestão usa o P80 da
UASG inteira. Para mudar o percentil ou a amostra mínima, altere `PERCENTIL_SUGESTAO` e `AMOSTRA_MINIMA`
em `server.py`.
