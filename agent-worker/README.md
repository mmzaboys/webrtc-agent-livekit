# agent-worker

AI agent worker. Here we implement the logic for the voice AI worker to explore different functions:
- fast-preresponse.py
This example uses Llama 3.1 8B and 70B. Initial quick response comes from 8B to optimize latency, and then the 70B takes over to handle the complex task.
- fast-preresponse-ollama.py
Same idea but using Ollama instead of Groq