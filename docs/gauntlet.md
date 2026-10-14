# Gauntlet Loop

Автономный цикл: агент правит код, пока не пройдёт объективная проверка.

## Установка агента (Ollama + aider, локально и бесплатно)

```bash
winget install Ollama.Ollama
ollama pull qwen2.5-coder:7b
pip install aider-chat
```

## Использование

```bash
bash scripts/gauntlet-loop.sh "Починить smoke" 10 "bash scripts/smoke.sh" "aider --model ollama/qwen2.5-coder:7b --yes --no-auto-commits"
```
