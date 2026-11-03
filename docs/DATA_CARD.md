# Data card

| Dataset | Used for | Size | License | Notes |
|---|---|---|---|---|
| **tau2-bench telecom** (sierra-research/tau2-bench) | Issue/root-cause/resolution models, KB for retrieval, case index, the honest held-out evaluation | 2,736 conversations · 2,285 tickets · 74 train / 40 test tasks (official split) | MIT | **LLM-simulated** support agent × customer simulator trajectories on scripted tech-support tasks; includes policy, tech-support manual and workflow (`data/kb/`) and a telecom DB (`data/reference/`) |
| **IBM Telco Customer Churn** | Tabular churn propensity (HGB, calibrated) | 7,043 customers, 26.5 % churn | Apache-2.0 | US-style survey/CRM attributes; no transcripts. Fused with conversation signals using expert priors |
| **Synthetic conversations** (`convoiq/data/synth.py`) | Coverage tau2 lacks: PII, fraud, injection, compliance violations, outages, code-switching, ASR noise | generated on demand (60-row sample in `data/samples/`) | Apache-2.0 | Template-derived: great for plumbing/regression, **not** evidence of real-world accuracy. All people/numbers are fabricated (Luhn-valid fake cards) |

## Why these sources
* tau2 telecom is the only public multi-turn, tool-grounded telecom support corpus with ground-truth tasks and a held-out split – so it gives *some* honest, non-template evaluation.
* No public dataset of real telecom call/chat transcripts with PII, compliance labels or churn outcomes exists that we could use legitimately; real data must be introduced under your own DPA (see MLOPS.md for the retraining path).
* Other sources considered: Bitext/customer-support intent datasets (generic, not telecom), Twitter/Reddit complaint dumps (licensing and PII), Telco churn variants on Kaggle (same distribution as IBM, redundant).

## Splits and leakage controls
* Models are trained **per task** with `GroupKFold`, never splitting a task across folds.
* `make train-eval` trains on the tau2 **train** tasks only → `models_eval/`; `make eval` scores that bundle on the **test** tasks. `make train` (all data) produces the production bundle in `models/`; its CV numbers are in `docs/MODEL_CARDS.md`.
* Synthetic evaluation uses different seeds from training but the same templates (optimistic by construction).

## Rebuilding the corpus
```
git clone https://github.com/sierra-research/tau2-bench ../tau2-bench
make data TAU2_DIR=../tau2-bench       # writes data/corpus/*.gz (untested against upstream layout changes)
```
## Sensitive content
tau2 and synthetic data contain no real people. If you add real transcripts, store them only in the raw zone, run them through the redactor before any training set is built, and record consent/retention basis in your own data register.
