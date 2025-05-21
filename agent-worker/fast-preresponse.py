import asyncio
import logging
import json
from collections.abc import AsyncIterable
from datetime import datetime

from dotenv import load_dotenv

from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    WorkerOptions,
    cli,
    llm,
    metrics,
    MetricsCollectedEvent
)
from livekit.agents.llm.chat_context import ChatContext, ChatMessage
from livekit.plugins import deepgram, groq, openai, silero
from prometheus_client import start_http_server, Summary, Counter, Gauge
from livekit.agents.metrics import LLMMetrics, STTMetrics, TTSMetrics, VADMetrics

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("pre-response-agent")

load_dotenv()

# Define Prometheus metrics
LLM_LATENCY = Summary('livekit_llm_duration_ms', 'LLM latency in milliseconds')
STT_LATENCY = Summary('livekit_stt_duration_ms', 'Speech-to-text latency in milliseconds')
TTS_LATENCY = Summary('livekit_tts_duration_ms', 'Text-to-speech latency in milliseconds')
TOTAL_TOKENS = Counter('livekit_total_tokens', 'Total tokens processed')
CONVERSATION_TURNS = Counter('livekit_conversation_turns', 'Number of conversation turns')
ACTIVE_CONVERSATIONS = Gauge('livekit_active_conversations', 'Number of active conversations')

class PreResponseAgent(Agent):
    def __init__(self):
        super().__init__(
            instructions="You are a helpful assistant",
            llm=groq.LLM(model="llama-3.3-70b-versatile"),
        )
        self._fast_llm = groq.LLM(model="llama-3.1-8b-instant")
        self._fast_llm_prompt = llm.ChatMessage(
            role="system",
            content=[
                "Generate a very short instant response to the user's message with 5 to 10 words.",
                "Do not answer the questions directly. Examples: OK, Hm..., let me think about that, "
                "wait a moment, that's a good question, etc.",
            ],
        )

    async def on_user_turn_completed(self, turn_ctx: ChatContext, new_message: ChatMessage):
        # Create a short "silence filler" response to quickly acknowledge the user's input
        fast_llm_ctx = turn_ctx.copy(
            exclude_instructions=True, exclude_function_call=True
        ).truncate(max_items=3)
        fast_llm_ctx.items.insert(0, self._fast_llm_prompt)
        fast_llm_ctx.items.append(new_message)

        # # Intentionally not awaiting SpeechHandle to allow the main response generation to
        # # run concurrently
        # self.session.say(
        #     self._fast_llm.chat(chat_ctx=fast_llm_ctx).to_str_iterable(),
        #     add_to_chat_ctx=False,
        # )

        # Alternatively, if you want the reply to be aware of this "silence filler" response,
        # you can await the fast llm done and add the message to the turn context. But note
        # that not all llm supports completing from an existing assistant message.

        fast_llm_fut = asyncio.Future[str]()

        async def _fast_llm_reply() -> AsyncIterable[str]:
            filler_response: str = ""
            async for chunk in self._fast_llm.chat(chat_ctx=fast_llm_ctx).to_str_iterable():
                filler_response += chunk
                yield chunk
            fast_llm_fut.set_result(filler_response)

        self.session.say(_fast_llm_reply(), add_to_chat_ctx=False)

        filler_response = await fast_llm_fut
        logger.info(f"Fast response: {filler_response}")
        turn_ctx.add_message(role="assistant", content=filler_response, interrupted=False)


async def entrypoint(ctx: JobContext):
    await ctx.connect()

    session = AgentSession(
        stt=deepgram.STT(),
        tts=openai.TTS(),
        vad=silero.VAD.load(),
    )
    
    usage_collector = metrics.UsageCollector()
    ACTIVE_CONVERSATIONS.inc()

    @session.on("metrics_collected")
    def handle_metrics(ev: MetricsCollectedEvent):
        # Log all metrics
        metrics.log_metrics(ev.metrics)
        usage_collector.collect(ev.metrics)
        
        # Track metrics based on their type
        if isinstance(ev.metrics, LLMMetrics):
            if hasattr(ev.metrics, 'duration_ms'):
                LLM_LATENCY.observe(ev.metrics.duration_ms)
            if hasattr(ev.metrics, 'total_tokens'):
                TOTAL_TOKENS.inc(ev.metrics.total_tokens)
                logger.info(
                    "LLM Metrics",
                    extra={
                        "latency_ms": getattr(ev.metrics, 'duration_ms', 0),
                        "total_tokens": ev.metrics.total_tokens,
                        "timestamp": datetime.utcnow().isoformat()
                    }
                )
        
        elif isinstance(ev.metrics, STTMetrics):
            if hasattr(ev.metrics, 'duration_ms'):
                STT_LATENCY.observe(ev.metrics.duration_ms)
                logger.info(
                    "STT Metrics",
                    extra={
                        "latency_ms": ev.metrics.duration_ms,
                        "timestamp": datetime.utcnow().isoformat()
                    }
                )
        
        elif isinstance(ev.metrics, TTSMetrics):
            if hasattr(ev.metrics, 'duration_ms'):
                TTS_LATENCY.observe(ev.metrics.duration_ms)
                logger.info(
                    "TTS Metrics",
                    extra={
                        "latency_ms": ev.metrics.duration_ms,
                        "timestamp": datetime.utcnow().isoformat()
                    }
                )
        
        elif isinstance(ev.metrics, VADMetrics):
            # Log VAD metrics without assuming specific attributes
            logger.info(
                "VAD Metrics",
                extra={
                    "metrics": str(ev.metrics),
                    "timestamp": datetime.utcnow().isoformat()
                }
            )
        
        # Track conversation metrics if available
        if hasattr(ev.metrics, 'conversation'):
            CONVERSATION_TURNS.inc()
            logger.info(
                "Conversation Metrics",
                extra={
                    "turn_count": CONVERSATION_TURNS._value.get(),
                    "timestamp": datetime.utcnow().isoformat()
                }
            )

    async def log_usage():
        summary = usage_collector.get_summary()
        # Convert UsageSummary to a dictionary of its attributes
        summary_dict = {
            "llm": {
                "total_tokens": getattr(summary.llm, 'total_tokens', 0),
                "duration_ms": getattr(summary.llm, 'duration_ms', 0)
            } if hasattr(summary, 'llm') else None,
            "stt": {
                "duration_ms": getattr(summary.stt, 'duration_ms', 0)
            } if hasattr(summary, 'stt') else None,
            "tts": {
                "duration_ms": getattr(summary.tts, 'duration_ms', 0)
            } if hasattr(summary, 'tts') else None
        }
        
        logger.info(
            "Session Summary",
            extra={
                "usage_summary": json.dumps(summary_dict),
                "active_conversations": ACTIVE_CONVERSATIONS._value.get(),
                "timestamp": datetime.utcnow().isoformat()
            }
        )
        ACTIVE_CONVERSATIONS.dec()

    await session.start(PreResponseAgent(), room=ctx.room)
    
    # Start up the server to expose the metrics.
    start_http_server(8000)
    ctx.add_shutdown_callback(log_usage)


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))