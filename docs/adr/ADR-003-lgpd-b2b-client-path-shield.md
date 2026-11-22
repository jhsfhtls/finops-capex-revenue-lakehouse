# ADR-003: Isolamento LGPD/B2B (POP_T_154), Client Path Shield, Defesa CWE-1236 e Delimitação MCP

- **Status:** Accepted
- **Date:** 2026-11-22
- **Governance & Standards:** `POP_T_154`, `LGPD (Lei 13.709/2018)`, `Client Path Shield`, `CWE-1236 (CSV/XLSX Formula Injection)`, `OWASP LLM01 (<untrusted_data>)`

## Context

O `finops-capex-revenue-lakehouse` atua simultaneamente em dois contextos complementares dentro do ecossistema C.O.R.E.:
1. Como **repositório público de portfólio** no GitHub, demonstrando arquitetura financeira, modelagem Star Schema, aging de inadimplência e FinOps sem jamais expor propriedade intelectual ou dados confidenciais de clientes, empregadores ou pessoas físicas.
2. Como **motor financeiro determinístico local** consumido por fluxos de consultoria B2B (`../operations/Clientes/<Cliente>/`), carreira (`../career/`) e estudos (`../studies/`), além de agentes autônomos via protocolo MCP.

Sem barreiras físicas e de código, planilhas externas de clientes poderiam ser gravadas acidentalmente dentro do diretório `data/` do repositório público, fórmulas maliciosas em planilhas (`CWE-1236`) poderiam comprometer exportações, ou textos não confiáveis poderiam induzir *Prompt Injection* (`OWASP LLM01`) em LLMs consumidores.

## Decision

Aplicamos quatro camadas obrigatórias de defesa e isolamento (*Defense-in-Depth*):

1. **Zero Dados Reais e Zero PII no Repositório Público (`POP_T_154` / LGPD):**
   - O repositório versiona e gera exclusivamente dados **sintéticos determinísticos** (`seed=42`) e séries macroeconômicas públicas (`PTAX`, `Selic`, `IPCA` obtidas de `../public-data-gateway` ou de snapshot público de referência).
   - É estritamente vedado versionar nomes reais de clientes, CNPJs reais de contrapartes privadas, contratos comerciais confidenciais, CPFs sem máscara ou dados proprietários de empregadores anteriores/atuais.

2. **Client Path Shield (Barreira Física de Gravação):**
   - A CLI `fcl`, o SDK `FinOpsLakehouse` e a API FastAPI validam qualquer caminho de entrada/saída classificado como carga real/externa (`client`, `career`, `operations`, `real_data`).
   - Qualquer tentativa de persistir artefatos reais dentro de `<repo_root>/data/` é **bloqueada em tempo de execução** com erro explícito de segurança, exigindo diretórios externos isolados (como `../operations/Clientes/<Cliente>/`, `../career/` ou `../studies/`).

3. **Defesa contra Formula Injection (`CWE-1236`) e Payloads Maliciosos:**
   - Na ingestão e exportação de CSV e XLSX, todo campo textual iniciado por caracteres executáveis (`=`, `+`, `-`, `@`, `\t`, `\r`) — exceto números decimais negativos válidos quando tipados como numéricos — é neutralizado antes da persistência ou exportação.
   - Arquivos compactados ou planilhas submetidos passam por verificação de *magic bytes* e limite de razão de expansão (`<= 100:1`) contra *Zip Bombs*.

4. **Isolamento de Segredos e Contenção MCP (`OWASP LLM01`):**
   - Nenhum código lê, imprime ou registra `.env` ou credenciais em logs/telemetria (`security_env_isolation.md`); clientes HTTP carregam configurações internamente e expõem apenas status booleano.
   - Respostas do servidor MCP `fcl-mcp` contendo descrições textuais ou campos livres oriundos de fontes externas são obrigatoriamente encapsuladas em delimitadores `<untrusted_data>...</untrusted_data>`.
   - Todas as dependências do projeto restringem-se a licenças OSI permissivas (`MIT`, `BSD`, `Apache-2.0`).

## Consequences

- Separação hermética entre portfólio público auditável e dados privados de operação/carreira, eliminando vazamento de IP ou PII em commits e PRs.
- Segurança verificável para ingestão de planilhas financeiras corporativas e consumo autônomo por agentes de IA via MCP, CLI, SDK e REST API.
