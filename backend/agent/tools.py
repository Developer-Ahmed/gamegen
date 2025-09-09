from typing import Any, Callable, List, Optional, cast
from langchain_tavily import TavilySearch
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg
from typing_extensions import Annotated
from langgraph.prebuilt import ToolNode
from .configuration import Configuration

import asyncio
from playwright.async_api import async_playwright

import requests

from e2b import Sandbox

# to use celery workers:
# from celery import shared_task
# add shared_task before the function 
# .delay when calling it

timeout = 60 * 20 # 20 minutes
sandbox = Sandbox("hz6cq1pkqd79v8kexebd", api_key="e2b_f57262ad11f118dd0a0be41796fc09ff2fc0f12f", timeout=timeout) # default: alive for 5 min

async def web_search(query: str, *, config: Annotated[RunnableConfig, InjectedToolArg]) -> Optional[list[dict[str, Any]]]:
    """Search for general web results.

    This function performs a search using the Tavily search engine, which is designed
    to provide comprehensive, accurate, and trusted results. It's particularly useful
    for answering questions about current events.
    """
    configuration = Configuration.from_runnable_config(config)
    wrapped = TavilySearch(max_results=configuration.max_search_results, tavily_api_key="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")
    result = await wrapped.ainvoke({"query": query})
    return cast(list[dict[str, Any]], result)

def write_to_file(file_path: str, file_content: str, *, config: Annotated[RunnableConfig, InjectedToolArg]):
    """
    Write to file in sandbox to prepare for testing.

    Args:
        file_path: Path of the file inside the sandbox
        file_content: Content of the file

    Returns:
        A string describing the result of writing to the file.

    Usage:
        Use this tool after generating source code for a file.
        Use the correct filepath that will fit the whole file structure.
    """
    res = sandbox.files.write(file_path, file_content)
    print(f"wrote to file res: {res}")
    return "success"

def compile_to_wasm(command: str, *, config: Annotated[RunnableConfig, InjectedToolArg]):
    """
    Compiles c++ code to WebAssembly.

    Args:
        command: emscipten command for compiling. Consider name of c++ files inside the sandbox. 
        And use the same output name.

    Returns:
        A string describing the result of writing to the file.

    Usage:
        Use this tool to compile c++ code into WebAssembly.
    """
    # res = sandbox.commands.run(command)
    # print(f"res: {result.exit_code}")
    res = sandbox.commands.run(f'bash -c "source /home/user/emsdk/emsdk_env.sh && {command}"')
    print(f"res: {res}")
    return "success"

async def run_game(config: Annotated[RunnableConfig, InjectedToolArg]):
    """
    Runs the game for testing.

    Args: None

    Returns:
        A success message or the error after running the game.

    Usage:
        Use this tool to test the game.
    """
    # res = sandbox.commands.run("python3 -m http.server 8001")
    # print(f"command res: {res}")
    # res = requests.get("http://localhost:8004/")
    # print(f"res: {res}")
    # Start a simple HTTP server inside the sandbox.
    # IMPORTANT NOTE: Getting a blocking error, so switch to async sandbox, but for now using --allow-blocking
    sandbox.commands.run("python3 -m http.server 3000 --directory .", background=True)
    host = sandbox.get_host(3000)
    url = f"https://{host}"
    print('Server started at:', url)
    # playwright error scraping
    console_err = []
    page_err = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        # Attach listeners BEFORE navigation
        def handle_console_errors(msg):
            nonlocal console_err
            err_str = f"[{msg.type}] {msg.text} "
            console_err.append(err_str)
        
        def handle_page_errors(exc):
            nonlocal page_err
            err_str = f"[PAGE ERROR] {exc.message} "
            page_err.append(err_str)

        page.on("console", handle_console_errors)
        page.on("pageerror", handle_page_errors)

        # Navigate to your WebAssembly/WebGL page
        await page.goto(f"{url}/web/", wait_until='domcontentloaded')

        # 🕒 Wait 5 seconds to allow full page load, scripts, rendering
        await asyncio.sleep(5)

        await browser.close()

    result = console_err + page_err
    if len(result) > 0:
        return result
    else:
        return "success"

TOOL_LIST: List[Callable[..., Any]] = [web_search, write_to_file, compile_to_wasm, run_game]
tool_node = ToolNode(TOOL_LIST)