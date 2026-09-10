"""Driven adapter: ModelGateway over LiteLLM.

Phase:   F0 (library) / D2 (proxy)
Tasks:   docs/TASKS.md#t-f0-04
Implements: ports/model_gateway.py

DAY 1 - LIBRARY MODE
    `base_url()` returns None. LiteLLM is imported in-process. No extra process, no extra
    database. Verified: LiteLLM's DB is optional; without it you still get the
    OpenAI-compatible surface, routing and failover.

DAY 2 - PROXY MODE
    `base_url()` returns the proxy URL. THAT IS THE ENTIRE CODE CHANGE. Virtual keys,
    per-tenant budgets and spend tracking then live in the proxy, not in our code.

    If day 2 turns out to touch anything else, ports/model_gateway.py was leaking and the
    fix belongs there, not here.

PROVIDER COVERAGE (verified): OpenRouter, Moonshot AI (Kimi), Z.AI (Zhipu/GLM), MiniMax,
DeepSeek, xAI, Groq, Together, Fireworks, Nebius, Ollama, vLLM - 100+, plus a JSON form
for any OpenAI-compatible endpoint.

WHAT "SUPPORTED" DOES NOT COVER
    Request format is translated; FAILURE MODES ARE NOT. An aggregator's upstream 429 and
    your own key's 429 arrive identical, and the right responses are opposite. That is
    what `classify_error` is for, and why it is scheduled late rather than guessed now.
"""
