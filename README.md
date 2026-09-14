# LocalProvider generation-budget diagnostic

Read-only diagnostic. It performs no Ollama inference and changes no production file.

Run from the Stage 3 project root:

python -m tools.diagnose_local_provider_generation_budget

It reports:
- exact LocalProvider source path and constructor signature
- relevant source lines for `num_predict`, `num_ctx`, `options`, `think`, timeout and reasoning
- AIRequest fields that could carry an output-token budget
- a conservative static verdict on whether an explicit output ceiling exists
