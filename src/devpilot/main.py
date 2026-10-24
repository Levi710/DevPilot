from llm_client import ask_llm

def main():
    task= (
        "you are helping diagnose software bugs."
        "explain in two sentences why input validation"
        "is important in a Python API."
    )

    answer =ask_llm(task)
    print("\nDevPilot response:\n")
    print(answer)

if __name__ == "__main__":
    main()