# ADR-001: Precisão Monetária Estrita em DECIMAL(18,4), Proibição de Float e Storage Híbrido Medallion

- **Status:** Accepted
- **Date:** 2026-11-22
- **Governance & Standards:** `DECIMAL(18,4)`, `Python decimal.Decimal (ROUND_HALF_EVEN / ABNT NBR 5891)`, `DuckDB + Apache Parquet (ZSTD)`, `SQLite WAL`

## Context

Sistemas analíticos e motores financeiros escritos em Python frequentemente recorrem a tipos nativos de ponto flutuante binário (`IEEE 754 float` / `float64`) para representar valores monetários, taxas de câmbio, juros e saldos contábeis. Em operações de razão contábil (CAPEX/OPEX em partidas dobradas), aging de recebíveis, cálculo de VPL/TIR e apuração tributária (Simples Nacional, INSS, IRPF, ISS), erros de representação binária (como `0.1 + 0.2 != 0.3`) geram desvios silenciosos de centavos que violam o fechamento contábil ($\sum \text{Débito} - \sum \text{Crédito} = 0$), corrompem a soma das faixas de inadimplência e introduzem inconsistências entre interfaces (SDK, CLI, REST, MCP e BI).

Adicionalmente, cargas analíticas locais com dezenas de milhares de lançamentos contábeis e simulações de Monte Carlo exigem alto desempenho sem depender de data warehouses em nuvem pagos.

## Decision

Adotamos precisão decimal exata ponta a ponta combinada com armazenamento híbrido Medallion local:

1. **Proibição Estrita de `float` em Valores Monetários e Taxas:**
   - Todos os cálculos monetários em Python utilizam exclusivamente `decimal.Decimal` com quantização explícita via `ROUND_HALF_EVEN` (critério bancário / ABNT NBR 5891).
   - Valores monetários em DuckDB, Apache Parquet e modelos dbt utilizam `DECIMAL(18,4)`.
   - Taxas financeiras, câmbio (`PTAX`), índices macroeconômicos (`Selic`, `IPCA`), alíquotas tributárias e probabilidades de transição (*roll-rates*) utilizam `DECIMAL(12,6)`.
   - A serialização entre SDK, CLI, FastAPI e MCP preserva precisão exata (valores em `Decimal` no SDK e strings decimais canonizadas nas fronteiras JSON/CLI/MCP), com testes automatizados rejeitando tipos `float` nas saídas financeiras.

2. **Simulação Monte Carlo Determinística Livre de `float`:**
   - A simulação de 10.000 cenários de paridade salarial (`CLT vs. PJ vs. Contractor USD/EUR`) executa com `seed=42` determinística sobre aritmética escalada em inteiros de 64 bits (`int64` em décimos de milésimo de unidade monetária, escala $10^{-4}$), convertendo os percentis e métricas finais diretamente para `Decimal('0.0001')`. Isso elimina desvios de ponto flutuante e mantém a execução abaixo de 1 segundo.

3. **Storage Híbrido Medallion (`Bronze` / `Silver` / `Gold`):**
   - **Bronze (Cache & Linhagem):** SQLite local em modo `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=NORMAL` e `busy_timeout=5000` para metadados operacionais, cache de séries públicas (`../public-data-gateway`) e registro de snapshots brutos selados com manifesto `SHA-256`.
   - **Silver & Gold (Lakehouse Analítico):** DuckDB embarcado materializando tabelas Apache Parquet comprimidas com `ZSTD` por *atomic swap* (arquivo `.tmp.parquet` seguido de rename atômico), alimentando modelos `dbt-duckdb` portáveis para Databricks e Snowflake.

## Consequences

- Reconciliação contábil e somatórios de aging exatos ao décimo de milésimo (`0.0001`), garantindo paridade matemática entre Python SDK, CLI `fcl`, API REST FastAPI, servidor MCP `fcl-mcp` e camada semântica Power BI / Streamlit.
- Execução local determinística, reprodutível e com custo zero de infraestrutura cloud, mantendo compatibilidade imediata de tipos `DECIMAL(18,4)` em motores enterprise.
