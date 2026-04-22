import os
import logging
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

def get_llm(task_type="complex"):
    load_dotenv()
    # Prefer OpenAI if available, otherwise fallback to Gemini
    openai_key = os.getenv("OPENAI_API_KEY")
    gemini_key = os.getenv("GOOGLE_API_KEY")

    if openai_key and openai_key != "your_openai_api_key":
        from langchain_openai import ChatOpenAI
        model_name = "gpt-4o" if task_type == "complex" else "gpt-4o-mini"
        logger.info(f"Using OpenAI LLM: {model_name}")
        return ChatOpenAI(model=model_name, temperature=0)
    elif gemini_key and gemini_key != "your_gemini_api_key":
        from langchain_google_genai import ChatGoogleGenerativeAI
        # Using Gemini 3 Flash for complex reasoning and 3.1 Flash Lite for fast tasks
        model_name = "gemini-3-flash-preview" if task_type == "complex" else "gemini-3.1-flash-lite-preview"
        logger.info(f"Using Gemini LLM: {model_name} (task: {task_type})")
        return ChatGoogleGenerativeAI(model=model_name, temperature=0)

    else:
        logger.error("No valid LLM API key found in .env")
        return None
