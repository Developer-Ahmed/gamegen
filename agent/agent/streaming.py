from langchain_core.messages import HumanMessage, AIMessage
from .graph import agent

import sys

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

if __name__ == "__main__":
    function = sys.argv[1]
    if function == "stream_chat":
        thread_id = sys.argv[2]
        msg = sys.argv[3]

        print(stream_chat(thread_id, msg))
    elif function == "get_final_or_full":
        thread_id = sys.argv[2]
        print(f"thread_id: {thread_id}")
        final = sys.argv[3]
        print(f"final: {final}")

        print(get_final_or_full(thread_id, final))
