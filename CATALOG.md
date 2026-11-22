<div align="center">
  <h2>Catálogo Mestre de Domínios — FinOps, CAPEX/OPEX & Revenue Lakehouse</h2>
  <p><i>Mapeamento Unificado de 6 Domínios Financeiros, Chaves Canônicas e Contratos ODCS v3.1</i></p>
  <p><b>Versão:</b> 1.0.0 &nbsp;|&nbsp; <b>Última Atualização:</b> 2026-11-22 &nbsp;|&nbsp; <b>Padrões:</b> ADR-001 • ADR-002 • ADR-003 • DECIMAL(18,4)</p>
  <hr/>
</div>
<br/>

# Catálogo Mestre de Domínios Financeiros (6 Domínios)

Este catálogo consolida os domínios analíticos e calculadoras determinísticas do `finops-capex-revenue-lakehouse`, garantindo precisão monetária estrita em `DECIMAL(18,4)` (`ADR-001`), chaves relacionais `STRING` com validação `ODCS v3.1` (`ADR-002`) e isolamento LGPD/B2B via *Client Path Shield* (`ADR-003`).

---

## 1. Domínios Catalogados & Ciclo de Entrega

| ID Domínio | Nome / Escopo | Fase | Precisão | SLA | Tier de Privacidade | Chaves Canônicas | Status |
| :--- | :--- | :---: | :---: | :---: | :--- | :--- | :---: |
| `capex_opex_ledger` | **Razao Contabil CAPEX/OPEX (Partida Dobrada)**<br/>*Controle orcamentario CAPEX vs. OPEX, variacoes orcado vs. realizado, sazonalidade e reconciliacao em partidas dobradas.* | **Fase 02** | `DECIMAL(18,4)` | `24h` | `SYNTHETIC_DETERMINISTIC` | `id_lancamento`<br/>`cod_centro_custo`<br/>`cod_conta_contabil`<br/>`id_contrato_masked` | `HOMOLOGATED` |
| `receivables_aging` | **Aging de Recebiveis & Recuperacao de Receita**<br/>*Motor de aging de inadimplencia, matriz Markoviana de roll-rate, perda esperada (ECL) e score de priorizacao de cobranca.* | **Fase 03** | `DECIMAL(18,4)` | `24h` | `SYNTHETIC_DETERMINISTIC` | `id_fatura_masked`<br/>`id_contrato_masked`<br/>`cnpj_14`<br/>`sigla_uf` | `CATALOGED` |
| `b2b_deal_economics` | **Economia de Negocios B2B (VPL, TIR, Payback & Margem)**<br/>*Calculos deterministicos de ROI, Payback, VPL (NPV), TIR (IRR), margem bruta (gate >= 65%) e custo da inacao.* | **Fase 04** | `DECIMAL(18,4)` | `24h` | `CLIENT_SHIELDED_CALC` | `id_contrato_masked`<br/>`cod_centro_custo` | `CATALOGED` |
| `career_tax_parity` | **Paridade Tributaria & Remuneracao (CLT x PJ x USD/EUR)**<br/>*Comparador liquido CLT vs. PJ (Simples Nacional Anexos III/V + Fator R + ISS Manaus) vs. Contractor USD/EUR com PTAX/Selic/IPCA.* | **Fase 04** | `DECIMAL(18,4)` | `24h` | `CAREER_SHIELDED_CALC` | `id_municipio`<br/>`sigla_uf` | `CATALOGED` |
| `public_macro_bridge` | **Ponte Macroeconomica Publica (PTAX, Selic, IPCA)**<br/>*Leitor read-only das series publicas PTAX, Selic e IPCA em ../public-data-gateway com snapshot offline de referencia (seed=42) para CI.* | **Fase 04** | `DECIMAL(12,6)` | `24h` | `PUBLIC_NON_PERSONAL` | `id_municipio`<br/>`sigla_uf` | `CATALOGED` |
| `ai_cloud_finops` | **AI & Cloud FinOps Telemetry (POP_T_158)**<br/>*Ingestao de telemetria de LLMs (tiers T1-T4), apuracao de custo por tarefa e simulador de economia por Prompt Cache, Batch API e janela Off-Peak.* | **Fase 05** | `DECIMAL(18,4)` | `1h` | `INTERNAL_TELEMETRY_REDACTED` | `id_lancamento`<br/>`cod_centro_custo` | `CATALOGED` |

---

## 2. Dicionário de Chaves Canônicas & Tipagem Estrita (`ADR-002`)

| Chave Canônica | Tipo Estrito | Padrão / Formato | Validação | Referência |
| :--- | :--- | :--- | :--- | :--- |
| `id_lancamento` | `STRING` | Chave unica deterministica do lancamento | Unicidade por lote e seed | ADR-002 |
| `cod_centro_custo` | `STRING` | Codigo canonico de centro de custo (ex.: 'CC-0100-ENG') | Prefixo e dominio validos | ADR-002 |
| `cod_conta_contabil` | `STRING` | Plano de contas hierarquico (ex.: '4.01.01.001') | Mascara contabil hierarquica | ADR-002 |
| `id_contrato_masked` | `STRING` | Identificador mascarado de contrato B2B | Tokenizacao sem PII real | ADR-002, ADR-003 |
| `id_fatura_masked` | `STRING` | Identificador mascarado de fatura/titulo | Tokenizacao sem PII real | ADR-002, ADR-003 |
| `cnpj_14` | `STRING(14)` | CNPJ canonico zfill 14D | Modulo 11 (2 digitos verificadores) | ADR-002, ADR-003 |
| `id_municipio` | `STRING(7)` | Codigo IBGE 7 digitos (ex.: '1302603') | Crosswalk IBGE / public-data-gateway | ADR-002 |
| `sigla_uf` | `STRING(2)` | UF oficial 2 letras maiusculas (ex.: 'AM') | Tabela IBGE das 27 UFs | ADR-002 |

---

## 3. Governança de Privacidade & Isolamento (`ADR-003`)

| Tier de Privacidade | Base Normativa | Regra de Isolamento | Domínios |
| :--- | :--- | :--- | :--- |
| `SYNTHETIC_DETERMINISTIC` | ADR-003 / POP_T_154 | Dados 100% sinteticos gerados com seed=42; zero dados de empresas reais. | `capex_opex_ledger, receivables_aging` |
| `CLIENT_SHIELDED_CALC` | ADR-003 / Client Path Shield | Motor deterministico puro; cargas reais externas proibidas em data/. | `b2b_deal_economics` |
| `CAREER_SHIELDED_CALC` | ADR-003 / Client Path Shield | Simulador CLT/PJ/USD; insumos pessoais permanecem em ../career/. | `career_tax_parity` |
| `INTERNAL_TELEMETRY_REDACTED` | POP_T_154 / POP_T_158 | Spans de telemetria LLM sem tokens/segredos ou CPFs. | `ai_cloud_finops` |
| `PUBLIC_NON_PERSONAL` | LAI / LGPD Art. 1 | Series macroeconomicas publicas (PTAX, Selic, IPCA). | `public_macro_bridge` |

---

## 4. Instruções de Manutenção & Verificação (`--check`)

O arquivo `CATALOG.md` é gerado de forma determinística a partir de `catalog/domains_registry.yaml`:

```powershell
python scripts/render_catalog.py --check
python scripts/render_catalog.py
```
