from typing import (
    Annotated,
    Sequence,
    TypedDict,
)
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.graph import StateGraph, END, START
from agent.state import InputState, State
from agent.configuration import Configuration

from agent.react_agent import create_react_agent
from .tools import TOOL_LIST, tool_node 

agent = create_react_agent("Coder Agent", TOOL_LIST, tool_node)








