import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

api_key=os.getenv("NVIDIA_API_KEY")
model= os.getenv("NVIDIA_MODEL")

if not api_key:
    raise ValueError(
        "NVIDIA_API_KEY is missing .check your .env file"
    )

if not model:
    raise ValueError(
        "NVIDIA_MODEL is missing. check your .env file."
    )

client = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=api_key,
)

def ask_llm(task: str) -> str:
    """Send a task to an NVIDIA-hosted model."""

    response= client.chat.completions.create(
        model=model,
        messages=[
            {"role":"user", "content":task}
        ],
        max_tokens=512,
    )

    content= response.choices[0].message.content

    if not content:
        raise RuntimeError("The model returned no text.")

    return content
    