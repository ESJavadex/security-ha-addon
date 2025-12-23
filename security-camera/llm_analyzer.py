#!/usr/bin/env python3
"""
LLM-based image analysis for false positive detection.
Analyzes composite images of recordings using vision LLMs to classify
motion events as real activity or false positives (lighting changes, shadows).
"""

import os
import base64
import json
import logging
import math
import requests
import threading
from io import BytesIO
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple
from dataclasses import dataclass, asdict

try:
    from PIL import Image
except ImportError:
    Image = None  # Will be checked at runtime

logger = logging.getLogger(__name__)

DEFAULT_PROMPT = """Door camera frames from apartment building hallway. Auto-lighting = some frames dark/bright.

OUTPUT ONLY JSON, nothing else:
{"has_activity":bool,"has_person":bool,"has_vehicle":bool,"has_animal":bool,"has_delivery":bool,"is_false_positive":bool,"confidence":"high/medium/low","description":"brief"}

has_activity=true if person/vehicle/animal/delivery. is_false_positive=true if only lighting/shadows."""


@dataclass
class LLMAnalysisResult:
    """Result of LLM analysis."""
    is_false_positive: bool
    confidence: str  # "high", "medium", "low"
    description: str
    analyzed_at: str  # ISO timestamp
    model_used: str
    has_activity: bool = False
    has_person: bool = False
    has_vehicle: bool = False
    has_animal: bool = False
    has_delivery: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


class LLMAnalyzer:
    """
    Analyzes security camera recordings using vision LLMs.

    Creates composite images from screenshots and sends them to an
    OpenAI-compatible API (LM Studio, OpenRouter, OpenAI) for analysis.
    """

    def __init__(
        self,
        api_url: str,
        model_name: str = "llava",
        api_key: Optional[str] = None,
        enabled: bool = True,
        auto_analyze: bool = False,
        custom_prompt: Optional[str] = None,
        timeout: int = 60,
        max_retries: int = 3,
    ):
        """
        Initialize LLM analyzer.

        Args:
            api_url: OpenAI-compatible API endpoint
            model_name: Model name/ID to use
            api_key: API key (required for OpenRouter/OpenAI, optional for LM Studio)
            enabled: Whether LLM analysis is enabled
            auto_analyze: Whether to automatically analyze new recordings
            custom_prompt: Custom prompt to use instead of default
            timeout: API request timeout in seconds
            max_retries: Number of retry attempts on failure
        """
        self.api_url = api_url
        self.model_name = model_name
        self.api_key = api_key
        self.enabled = enabled
        self.auto_analyze = auto_analyze
        self.prompt = custom_prompt or DEFAULT_PROMPT
        self.timeout = timeout
        self.max_retries = max_retries

        # Track pending analyses
        self._pending_analyses: set = set()
        self._lock = threading.Lock()

        if Image is None:
            logger.warning("Pillow not installed - composite image creation disabled")

    def create_composite_image(
        self,
        screenshot_paths: List[Path],
        max_width: int = 1920,
        max_height: int = 1080,
    ) -> List[bytes]:
        """
        Create composite image(s) from screenshots.

        Arranges screenshots in a grid, resizing as needed to fit within
        the maximum dimensions. If there are too many screenshots for one
        image, creates multiple composites.

        Args:
            screenshot_paths: List of paths to screenshot images
            max_width: Maximum composite width
            max_height: Maximum composite height

        Returns:
            List of composite images as PNG bytes (usually 1, max 2)
        """
        if Image is None:
            raise RuntimeError("Pillow not installed")

        if not screenshot_paths:
            raise ValueError("No screenshots provided")

        # Load all images
        images = []
        for path in screenshot_paths:
            try:
                img = Image.open(path)
                images.append(img)
            except Exception as e:
                logger.warning(f"Could not load screenshot {path}: {e}")

        if not images:
            raise ValueError("Could not load any screenshots")

        # Calculate optimal grid layout
        # Max 12 images per composite to keep resolution reasonable
        max_per_composite = 12
        composites = []

        for batch_start in range(0, len(images), max_per_composite):
            batch = images[batch_start:batch_start + max_per_composite]
            composite = self._create_single_composite(batch, max_width, max_height)
            composites.append(composite)

            # Limit to 2 composites max
            if len(composites) >= 2:
                break

        return composites

    def _create_single_composite(
        self,
        images: List['Image.Image'],
        max_width: int,
        max_height: int,
    ) -> bytes:
        """Create a single composite image from a batch of images."""
        n = len(images)

        # Calculate grid dimensions
        cols = math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)

        # Calculate cell size to fit within max dimensions
        cell_width = max_width // cols
        cell_height = max_height // rows

        # Maintain aspect ratio of original images
        sample_img = images[0]
        aspect = sample_img.width / sample_img.height

        # Adjust cell size to maintain aspect ratio
        if cell_width / cell_height > aspect:
            cell_width = int(cell_height * aspect)
        else:
            cell_height = int(cell_width / aspect)

        # Create composite canvas
        composite_width = cols * cell_width
        composite_height = rows * cell_height
        composite = Image.new('RGB', (composite_width, composite_height), (0, 0, 0))

        # Paste resized images into grid
        for idx, img in enumerate(images):
            row = idx // cols
            col = idx % cols

            # Resize image to fit cell
            resized = img.resize((cell_width, cell_height), Image.Resampling.LANCZOS)

            # Paste into position
            x = col * cell_width
            y = row * cell_height
            composite.paste(resized, (x, y))

        # Convert to PNG bytes
        buffer = BytesIO()
        composite.save(buffer, format='PNG', optimize=True)
        return buffer.getvalue()

    def analyze_recording(
        self,
        recording_filename: str,
        screenshots: List[str],
        recordings_path: Path,
        save_composite: bool = False,
    ) -> LLMAnalysisResult:
        """
        Analyze a recording's screenshots using the LLM.

        Args:
            recording_filename: Name of the recording file
            screenshots: List of screenshot filenames
            recordings_path: Path to recordings directory
            save_composite: Whether to save the composite image to disk

        Returns:
            LLMAnalysisResult with classification
        """
        if not self.enabled:
            return LLMAnalysisResult(
                is_false_positive=False,
                confidence="low",
                description="LLM analysis disabled",
                analyzed_at=datetime.now().isoformat(),
                model_used=self.model_name,
                error="LLM analysis is disabled"
            )

        if not screenshots:
            return LLMAnalysisResult(
                is_false_positive=False,
                confidence="low",
                description="No screenshots available",
                analyzed_at=datetime.now().isoformat(),
                model_used=self.model_name,
                error="No screenshots to analyze"
            )

        logger.info(f"Analyzing recording: {recording_filename} ({len(screenshots)} screenshots)")

        try:
            # Build full paths to screenshots
            screenshot_paths = [
                recordings_path / screenshot
                for screenshot in screenshots
                if (recordings_path / screenshot).exists()
            ]

            if not screenshot_paths:
                raise ValueError("No valid screenshot files found")

            # Create composite image(s)
            composites = self.create_composite_image(screenshot_paths)

            # Save composite image(s) to disk for debugging/review
            if save_composite:
                base_name = recording_filename.replace('.mp4', '')
                for i, composite_bytes in enumerate(composites):
                    suffix = f"_part{i+1}" if len(composites) > 1 else ""
                    composite_path = recordings_path / f"{base_name}_composite{suffix}.png"
                    with open(composite_path, 'wb') as f:
                        f.write(composite_bytes)
                    logger.info(f"Saved composite image: {composite_path}")

            # Analyze each composite and combine results
            results = []
            for i, composite_bytes in enumerate(composites):
                result = self._call_llm_api(composite_bytes, i + 1, len(composites))
                results.append(result)

            # If any composite shows real activity, it's not a false positive
            is_false_positive = all(r.get("is_false_positive", False) for r in results)

            # Use lowest confidence if mixed results
            confidences = [r.get("confidence", "low") for r in results]
            confidence_order = {"high": 2, "medium": 1, "low": 0}
            min_confidence = min(confidences, key=lambda c: confidence_order.get(c, 0))

            # Combine descriptions
            descriptions = [r.get("description", "") for r in results]
            description = " | ".join(filter(None, descriptions))

            # Aggregate detection flags - true if ANY composite detected it
            has_activity = any(r.get("has_activity", False) for r in results)
            has_person = any(r.get("has_person", False) for r in results)
            has_vehicle = any(r.get("has_vehicle", False) for r in results)
            has_animal = any(r.get("has_animal", False) for r in results)
            has_delivery = any(r.get("has_delivery", False) for r in results)

            return LLMAnalysisResult(
                is_false_positive=is_false_positive,
                confidence=min_confidence,
                description=description,
                analyzed_at=datetime.now().isoformat(),
                model_used=self.model_name,
                has_activity=has_activity,
                has_person=has_person,
                has_vehicle=has_vehicle,
                has_animal=has_animal,
                has_delivery=has_delivery,
            )

        except Exception as e:
            logger.error(f"LLM analysis failed for {recording_filename}: {e}")
            return LLMAnalysisResult(
                is_false_positive=False,  # Safe default: keep recording
                confidence="low",
                description="Analysis failed",
                analyzed_at=datetime.now().isoformat(),
                model_used=self.model_name,
                error=str(e)
            )

    def _call_llm_api(
        self,
        image_bytes: bytes,
        composite_num: int = 1,
        total_composites: int = 1,
    ) -> dict:
        """
        Call the OpenAI-compatible vision API.

        Args:
            image_bytes: PNG image as bytes
            composite_num: Which composite this is (for multi-composite recordings)
            total_composites: Total number of composites

        Returns:
            Parsed JSON response from LLM
        """
        # Encode image as base64
        image_b64 = base64.b64encode(image_bytes).decode('utf-8')

        # Build prompt
        prompt = self.prompt
        if total_composites > 1:
            prompt += f"\n\n(This is part {composite_num} of {total_composites} from a longer recording)"

        # Build request payload (OpenAI vision format)
        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_b64}"
                            }
                        }
                    ]
                }
            ],
            "temperature": 0.1,  # Low temperature for consistent classification
        }

        # Build headers
        headers = {
            "Content-Type": "application/json"
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        # Retry logic
        last_error = None
        for attempt in range(self.max_retries):
            try:
                response = requests.post(
                    self.api_url,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout
                )
                response.raise_for_status()

                result = response.json()

                # Extract content from response
                content = result.get("choices", [{}])[0].get("message", {}).get("content", "")

                # Parse JSON from response
                return self._parse_llm_response(content)

            except requests.exceptions.Timeout:
                last_error = f"Request timeout after {self.timeout}s"
                logger.warning(f"LLM API timeout (attempt {attempt + 1}/{self.max_retries})")
            except requests.exceptions.RequestException as e:
                last_error = str(e)
                logger.warning(f"LLM API error (attempt {attempt + 1}/{self.max_retries}): {e}")
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Unexpected error (attempt {attempt + 1}/{self.max_retries}): {e}")

        raise RuntimeError(f"LLM API failed after {self.max_retries} attempts: {last_error}")

    def _parse_llm_response(self, content: str) -> dict:
        """
        Parse JSON from LLM response.

        Handles various response formats including markdown code blocks, thinking tags, etc.
        """
        import re

        # Try to extract JSON from response
        content = content.strip()

        # Remove <think>...</think> blocks (some models use this)
        # Also handle unclosed <think> tags (truncated responses)
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)
        content = re.sub(r'<think>.*', '', content, flags=re.DOTALL)

        # Remove markdown code blocks if present
        if "```" in content:
            # Extract content between code blocks
            code_match = re.search(r'```(?:json)?\s*([\s\S]*?)```', content)
            if code_match:
                content = code_match.group(1)
            else:
                # Just remove the backticks
                content = content.replace("```json", "").replace("```", "")

        content = content.strip()

        # Try to find JSON object in response
        try:
            # Look for JSON object
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                json_str = content[start:end]
                return json.loads(json_str)
        except json.JSONDecodeError:
            pass

        # If parsing fails, return default with raw response as description
        logger.warning(f"Could not parse LLM response as JSON: {content[:200]}")
        return {
            "is_false_positive": False,
            "confidence": "low",
            "description": content[:500] if content else "Could not parse response"
        }

    def is_analysis_pending(self, filename: str) -> bool:
        """Check if analysis is pending for a recording."""
        with self._lock:
            return filename in self._pending_analyses

    def mark_analysis_started(self, filename: str):
        """Mark that analysis has started for a recording."""
        with self._lock:
            self._pending_analyses.add(filename)

    def mark_analysis_complete(self, filename: str):
        """Mark that analysis has completed for a recording."""
        with self._lock:
            self._pending_analyses.discard(filename)

    def test_connection(self) -> Tuple[bool, str]:
        """
        Test connection to LLM API.

        Returns:
            Tuple of (success, message)
        """
        try:
            # Simple test request
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            # Try a minimal request
            payload = {
                "model": self.model_name,
                "messages": [{"role": "user", "content": "test"}],
                "max_tokens": 1,
            }

            response = requests.post(
                self.api_url,
                headers=headers,
                json=payload,
                timeout=10
            )

            if response.status_code == 200:
                return True, f"Connected to {self.api_url}"
            else:
                return False, f"API returned status {response.status_code}: {response.text[:200]}"

        except requests.exceptions.Timeout:
            return False, "Connection timeout"
        except requests.exceptions.ConnectionError:
            return False, "Could not connect to API"
        except Exception as e:
            return False, str(e)


# For standalone testing
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Test with environment variables
    api_url = os.environ.get("LLM_API_URL", "http://localhost:1234/v1/chat/completions")
    api_key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "llava")

    analyzer = LLMAnalyzer(
        api_url=api_url,
        model_name=model,
        api_key=api_key if api_key else None,
    )

    # Test connection
    success, message = analyzer.test_connection()
    print(f"Connection test: {success} - {message}")
