from typing import (
    Annotated,
    Sequence,
    TypedDict,
)
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, END, START
from langgraph.prebuilt import ToolNode
import agent.prompts as prompts
from typing import List, Callable, Any, Dict
from typing_extensions import Literal, cast

from langchain_openai import ChatOpenAI

from .configuration import Configuration
from .state import InputState, State

from langgraph.checkpoint.memory import InMemorySaver

def create_react_agent(agent_name: str, tool_list: List[Callable[..., Any]], tool_node: ToolNode) -> StateGraph:
    """Create a React agent.

    Args:
        agent_name (str): The name of the agent.

    Returns:
        StateGraph: The compiled graph. 
    """
    
    def call_model(state: State, config: RunnableConfig) -> Dict[str, List[AIMessage]]:
        """Call the LLM powering our "agent".

        This function prepares the prompt, initializes the model, and processes the response.

        Args:
            state (State): The current state of the conversation.
            config (RunnableConfig): Configuration for the model run.
            store (BaseStore): Database of relevant information for the conversation.

        Returns:
            dict: A dictionary containing the model's response message.
        """
        configuration = Configuration.from_runnable_config(config)

        # Initialize the model with tool binding. Change the model or add more tools here.
        model = ChatOpenAI(model="gpt-4.1", temperature=0.2)
        model = model.bind_tools(tool_list)

        response = cast(AIMessage, model.invoke([{"role": "system", "content": configuration.system_prompt}, *state.messages], config))

        # Handle the case when it's the last step and the model still wants to use a tool
        if state.is_last_step and response.tool_calls:
            return {
                "messages": [
                    AIMessage(
                        id=response.id,
                        content="Sorry, I could not find an answer to your question in the specified number of steps.",
                    )
                ]
            }

        # Return the model's response as a list to be added to existing messages
        return {"messages": [response]}

    def route_model_output(state: State) -> Literal["__end__", "tools"]:
        """Determine the next node based on the model's output.

        This function checks if the model's last message contains tool calls.

        Args:
            state (State): The current state of the conversation.

        Returns:
            str: The name of the next node to call ("__end__" or "tools").
        """
        last_message = state.messages[-1]
        if not isinstance(last_message, AIMessage):
            raise ValueError(
                f"Expected AIMessage in output edges, but got {type(last_message).__name__}"
            )
        # If there is no tool call, then we finish
        if not last_message.tool_calls:
            return END
        # Otherwise we execute the requested actions
        print("Tool calls:", last_message.tool_calls)
        return "tools"
        
    # Define a new graph
    workflow = StateGraph(State, input=InputState, config_schema=Configuration)

    # Define the two nodes we will cycle between
    workflow.add_node("agent", call_model)
    workflow.add_node("tools", tool_node)

    # Set the entrypoint as `agent`
    # This means that this node is the first one called
    workflow.add_edge(START, "agent")

    # We now add a conditional edge
    workflow.add_conditional_edges(
        # First, we define the start node. We use `agent`.
        # This means these are the edges taken after the `agent` node is called.
        "agent",
        # Next, we pass in the function that will determine which node is called next.
        route_model_output,
    )

    # We now add a normal edge from `tools` to `agent`.
    # This means that after `tools` is called, `agent` node is called next.
    workflow.add_edge("tools", "agent")

    checkpointer = InMemorySaver()

    # Now we can compile and visualize our graph
    graph = workflow.compile(checkpointer=checkpointer)
    graph.name = agent_name

    return graph