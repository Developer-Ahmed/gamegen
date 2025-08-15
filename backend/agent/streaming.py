from langchain_core.messages import HumanMessage, AIMessage
from .graph import agent

def stream_chat(thread_id: str, message: str):
    final = ""
    for chunk in agent.stream(
        {"messages": [{"role": "user", "content": message}]},
        config={"configurable": {"thread_id": thread_id}},
        stream_mode="updates"
    ):
        final = chunk
    return final

def get_final_or_full(thread_id: str, final: bool = True):
    full_history = agent.get_state(config={"configurable": {"thread_id": thread_id}})
    print(f"full history: {full_history}")
    messages = full_history.values.get("messages", {})
    print(f"messages: {messages}")
    if not messages:
        return []
    if final:
        return messages[-1].content
    else:
        return messages

