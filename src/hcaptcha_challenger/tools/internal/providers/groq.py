# -*- coding: utf-8 -*-
"""
GroqProvider - Groq API implementation using the official Groq SDK.

This provider provides a high-quota alternative to Gemini for image-based 
content generation, specifically optimized for Groq's Vision models.
"""
import base64
import json
from pathlib import Path
from typing import List, Type, TypeVar, Any

from groq import Groq, AsyncGroq
from loguru import logger
from pydantic import BaseModel
from hcaptcha_challenger.agent.logger import LoggerHelper
from tenacity import retry, stop_after_attempt, wait_fixed

ResponseT = TypeVar("ResponseT", bound=BaseModel)


class GroqProvider:
    """
    Groq-based chat provider implementation using the official SDK.
    """

    def __init__(self, api_key: str | List[str], model: str | List[str] = "meta-llama/llama-4-scout-17b-16e-instruct"):
        """
        Initialize the Groq provider.

        Args:
            api_key: Groq API key or list of keys.
            model: Model name or list of model names to use.
        """
        if isinstance(api_key, str):
            self._api_keys = [api_key] if api_key else []
        else:
            self._api_keys = [k for k in api_key if k] if api_key else []
        
        if isinstance(model, str):
            self._models = [model] if model else ["meta-llama/llama-4-scout-17b-16e-instruct"]
        else:
            self._models = [m for m in model if m] if model else ["meta-llama/llama-4-scout-17b-16e-instruct"]
        
        if not self._api_keys:
            LoggerHelper.log_error("GroqProvider: No valid API keys provided!", emoji='skull')
            # Fallback to avoid IndexError, though it will still fail to call
            self._api_keys = ["MISSING_KEY"]
            
        self._key_index = 0
        self._model_index = 0
        self._last_raw_response: Any = None

    @property
    def model(self) -> str:
        """Get the current active model name."""
        if not self._models:
            return "meta-llama/llama-4-scout-17b-16e-instruct"
        return self._models[self._model_index % len(self._models)]

    def rotate_key(self):
        """Rotate to the next API key in the list. If all keys used, rotate model."""
        if len(self._api_keys) > 1:
            self._key_index = (self._key_index + 1) % len(self._api_keys)
            LoggerHelper.log_info(f"Rotating Groq API key. New index: {self._key_index}", emoji='refresh')
            
            if self._key_index == 0 and len(self._models) > 1:
                self.rotate_model()
        elif len(self._models) > 1:
            self.rotate_model()

    def rotate_model(self):
        """Rotate to the next model in the list."""
        if len(self._models) > 1:
            self._model_index = (self._model_index + 1) % len(self._models)
            LoggerHelper.log_info(f"Rotating Groq model. New model: {self.model}", emoji='refresh')

    @property
    def api_key(self) -> str:
        if not self._api_keys:
            return "MISSING_KEY"
        return self._api_keys[self._key_index % len(self._api_keys)]

    def _encode_image(self, image_path: Path) -> str:
        """Encode image to base64 string."""
        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode("utf-8")

    @retry(
        stop=stop_after_attempt(15),  # 3 cycles of (keys * models)
        wait=wait_fixed(5),
        before_sleep=lambda retry_state: LoggerHelper.log_provider_error(
            retry_state.attempt_number, 15, retry_state.outcome.exception()
        ),
    )
    async def generate_with_images(
        self,
        *,
        images: List[Path],
        response_schema: Type[ResponseT],
        user_prompt: str | None = None,
        description: str | None = None,
        **kwargs,
    ) -> ResponseT:
        """
        Generate content with image inputs using Groq SDK.
        """
        if self.api_key == "MISSING_KEY":
            raise ValueError("Groq API Key is missing. Check your configuration.")

        client = AsyncGroq(api_key=self.api_key)
        
        content = []
        if user_prompt:
            content.append({"type": "text", "text": user_prompt})
        
        for img_path in images:
            if img_path.exists():
                base64_image = self._encode_image(img_path)
                mime_type = "image/png" if img_path.suffix.lower() == ".png" else "image/jpeg"
                content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{base64_image}"
                    }
                })

        messages = []
        if description:
            messages.append({"role": "system", "content": description})
        
        messages.append({"role": "user", "content": content})

        # Inject JSON schema instruction if applicable
        schema_instruction = ""
        if response_schema and issubclass(response_schema, BaseModel):
            try:
                schema = response_schema.model_json_schema()
                schema_instruction = f" Respond strictly in JSON format matching this schema: {json.dumps(schema)}"
                # Groq's JSON mode requires "JSON" to be in the prompt
                if "JSON" not in (user_prompt or ""):
                    messages.append({"role": "user", "content": f"The final response MUST be in JSON format.{schema_instruction}"})
            except Exception as e:
                logger.warning(f"Failed to generate JSON schema: {e}")

        try:
            chat_completion = await client.chat.completions.create(
                messages=messages,
                model=self.model,
                response_format={"type": "json_object"} if response_schema else None,
                temperature=0.0,
                max_tokens=1024,
            )
            
            self._last_raw_response = chat_completion
            
            if not chat_completion.choices:
                raise ValueError("Groq API returned an empty choices list (no response generated)")
                
            result_text = chat_completion.choices[0].message.content
            
            if not result_text:
                raise ValueError("Empty content in Groq API response")
                
            result_json = json.loads(result_text)
            
            # Post-processing for coordinate normalization (Llama 3.2 Vision quirk)
            if "coordinates" in result_json and isinstance(result_json["coordinates"], list):
                for coord in result_json["coordinates"]:
                    if "box_2d" in coord and isinstance(coord["box_2d"], list):
                        coord["box_2d"] = [int(v) if str(v).isdigit() else v for v in coord["box_2d"][:4]]
            
            return response_schema(**result_json)

        except Exception as e:
            err_msg = str(e).lower()
            if "429" in err_msg or "rate_limit" in err_msg:
                LoggerHelper.log_warning(f"Groq Rate Limit (429). Rotating key...", emoji='⏳')
                self.rotate_key()
            elif "400" in err_msg:
                 LoggerHelper.log_error(f"Groq API Error 400: {e}. Check model support for vision.")
            
            LoggerHelper.log_error(f"Groq Provider Error: {e}")
            raise e

    def cache_response(self, path: Path) -> None:
        """Cache the last response to a file."""
        if not self._last_raw_response:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Serialize the completion object to dict for saving
            output = {
                "id": self._last_raw_response.id,
                "model": self._last_raw_response.model,
                "choices": [
                    {
                        "message": {
                            "role": c.message.role,
                            "content": c.message.content
                        },
                        "finish_reason": c.finish_reason
                    } for c in self._last_raw_response.choices
                ]
            }
            path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            LoggerHelper.log_warning(f"Failed to save response cache: {e}")
