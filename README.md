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

### Régua por porte + curva ABC (modo padrão)

1. **Porte:** os órgãos são ordenados pelo volume estimado na janela (padrão de 365 dias) e divididos em
   tercis: Grande, Médio e Pequeno. O porte de qualquer órgão pode ser trocado manualmente.
2. **Régua da classe:** é o corte da curva ABC das compras de todos os órgãos do porte. Entram as maiores
   compras que, somadas, chegam a X% do valor da classe (padrão de 80%).
3. **Garantia:** se a régua do porte ficar acima da maior compra de um órgão, a régua dele cai para essa
   maior compra. Assim nenhum órgão fica sem análise.
4. **Piso:** nenhuma régua fica abaixo do limite padrão da UASG (materialidade mínima).
5. Um limite manual no órgão sempre prevalece.

A tela de alçadas mostra uma simulação dos cortes de 70%, 80%, 90% e 95%, com e sem garantia. Para cada um
ela informa as compras selecionadas, o valor coberto, quantos órgãos ficam sem nenhuma análise e quantas
licitações em andamento passam pela régua.
O modo "limite fixo" continua disponível e aplica apenas o limite padrão da UASG.
