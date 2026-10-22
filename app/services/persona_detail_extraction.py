import openai
import json
import os
from typing import Any, Dict
from loguru import logger

async def extract_persona_details_from_prompt(
    prompt_text: str,
) -> Dict[str, Any]:
    """
    Analyzes a prompt to extract persona details like name, gender, description, and characteristics.
    """
    prompt = f'''Analyze the following text prompt and extract the persona's name, gender, a brief description for image generation, and a list of characteristics.
If no explicit name is mentioned, generate a concise, descriptive name that clearly indicates the persona's role or key trait, based on the description and characteristics. This name should *not* be a personal name unless explicitly provided in the prompt. For example, if the description is about an interviewer, the name could be 'Interviewer Bot'.

Prompt: {prompt_text}

Extract the information in the following JSON format. If a field cannot be determined, use null or an empty list:
{{
    "name": "string or null",
    "gender": "male, female, non-binary, or null",
    "description": "string or null",
    "characteristics": ["string", ...]
}}

JSON Output:'''

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("OPENAI_API_KEY environment variable not set.")
        return None

    try:
        # Use async client
        client = openai.AsyncOpenAI(api_key=api_key)

        response = await client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": prompt},
            ],
        )

        content = response.choices[0].message.content
        if content:
            extracted_data = json.loads(content)
            
            # Apply default values and handle nulls/empty strings
            extracted_data["name"] = extracted_data.get("name") or "Bot"
            extracted_data["gender"] = extracted_data.get("gender") or "male"

            extracted_data['description'] = extracted_data.get("description") or prompt_text

            # Ensure characteristics is a list, default to empty list if null or not list
            characteristics = extracted_data.get("characteristics")
            extracted_data["characteristics"] = characteristics if isinstance(characteristics, list) else []
            
            return extracted_data
        else:
            logger.warning("LLM returned empty content for persona details extraction.")
            return None

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON from LLM response: {e}")
        return None
    except openai.AuthenticationError as e:
        logger.error(f"OpenAI Authentication Error: {e}. Please check your API key.")
        return None
    except Exception as e:
        logger.error(f"Error during LLM persona details extraction: {e}")
        return None 