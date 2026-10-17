"""Template for plugging your own agent in:

    agentguard test --agent examples.my_agent:agent --scenario duplicate-after-timeout

An agent is any object with run(task, tools) -> AgentResult, or a plain function with that shape.
`task.prompt` is the natural-language instruction; `tools` is the only way to act on the world.
"""

from agentguard.agents import AgentResult, Task
from agentguard.sandbox import Toolbox


def agent(task: Task, tools: Toolbox) -> AgentResult:
    # Replace with your LLM loop: expose tools.search / tools.checkout / tools.order_history
    # as function-calling tools and let the model decide.
    order = tools.checkout(task.params["sku"], 1)
    return AgentResult("success", order["order_id"])
