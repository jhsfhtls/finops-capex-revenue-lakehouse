# ADR-002: Chaves Canônicas STRING, Contratos ODCS v3.1, Invariantes de Partida Dobrada e Quarentena

- **Status:** Accepted
- **Date:** 2026-11-22
- **Governance & Standards:** `ODCS v3.1`, `Double-Entry Accounting Invariants`, `5-Pillar Data Observability`, `Canonical STRING Keys`

## Context

Pipelines financeiros e contábeis frequentemente sofrem de corrupção silenciosa de dados (*Silent Data Corruption*) causada por três fatores recorrentes:
1. Inferência automática de esquemas em CSV/XLSX que converte códigos hierárquicos e identificadores fiscais (`cod_centro_custo`, `cod_conta_contabil`, `cnpj_14`, `id_municipio`) em inteiros ou floats, destruindo zeros à esquerda, pontos estruturais e chaves de junção.
2. Ausência de validação formal antes da promoção entre camadas `Bronze` e `Silver`, permitindo que lotes contábeis desbalanceados ou faturas com saldos incoerentes contaminem dashboards executivos e calculadoras B2B.
3. Códigos sentinela numéricos (`99`, `999`, `-1`) e literais inválidos (`"SEM INFORMACAO"`, `"N/A"`) tratados como categorias ou valores financeiros válidos.

## Decision

Estabelecemos governança contratual estrita inspirada no padrão `ODCS v3.1` e nos 5 Pilares de Observabilidade de Dados:

1. **Chaves Canônicas Estritas em `STRING`:**
   - Todos os identificadores relacionais, contábeis e territoriais trafegam exclusivamente como `STRING` normalizada:
     - `id_lancamento` (`STRING`): identificador único determinístico do lançamento contábil.
     - `cod_centro_custo` (`STRING`): código canônico do centro de custo (ex.: `"CC-0100-ENG"`).
     - `cod_conta_contabil` (`STRING`): código estruturado do plano de contas (ex.: `"4.01.01.001"`).
     - `id_contrato_masked` (`STRING`): identificador mascarado/sintético de contrato B2B.
     - `id_fatura_masked` (`STRING`): identificador mascarado/sintético de título ou fatura a receber.
     - `cnpj_14` (`STRING(14)`): CNPJ canônico com preenchimento de zeros à esquerda (`zfill(14)`).
     - `id_municipio` (`STRING(7)`) e `sigla_uf` (`STRING(2)`): chaves territoriais padronizadas em alinhamento com `../public-data-gateway`.

2. **Contratos Declarativos `contract.yaml` (ODCS v3.1) e 5 Pilares:**
   - Cada domínio financeiro possui um arquivo declarativo `contract.yaml` especificando esquema tipado, chaves primárias, unidades monetárias ISO-4217 (`BRL`, `USD`, `EUR`), mapeamento determinístico de sentinelas (`99`, `999`, `-1`, `"SEM INFORMACAO"`) para `NULL` e limiares dos 5 Pilares (*Freshness, Volume, Schema, Null Ceiling, Lineage SHA-256*).

3. **Invariantes Financeiros Obrigatórios e Zona de Quarentena:**
   - **Partida Dobrada:** Todo lote de razão contábil deve satisfazer $\sum \text{Débito} - \sum \text{Crédito} = 0.0000$ no nível de lote e período contábil.
   - **Consistência de Recebíveis:** Faturas exigem `valor_original > 0`, `0 <= valor_em_aberto <= valor_original` e identidade exata entre o saldo total da carteira e a soma das faixas de aging (`a_vencer`, `0-30`, `31-60`, `61-90`, `91-180`, `>180`).
   - Qualquer lote ou registro que viole o contrato `ODCS v3.1` ou os invariantes contábeis é desviado atomicamente para `data/<dominio>/quarantine/` com relatório estruturado de violações, preservando íntegro o snapshot `Silver` anterior.

## Consequences

- Bloqueio determinístico de dados inconsistentes antes que alcancem modelos `dbt` Gold, APIs ou relatórios gerenciais.
- Rastreabilidade completa por manifesto `SHA-256` e auditoria explicável para cada registro rejeitado em quarentena.
