# -*- coding: utf-8 -*-
"""
SpatialPathReasoner - Drag and drop challenge solver.

This tool analyzes images to identify which draggable element should be
moved to which target location based on visual patterns and implicit matching rules.
"""
from pathlib import Path
from typing import Union, Optional

from hcaptcha_challenger.models import ImageDragDropChallenge, ChallengeTypeEnum
from hcaptcha_challenger.tools.spatial.base import SpatialReasoner
from hcaptcha_challenger.utils import load_desc


# Mapping of challenge types to their specific prompt files
PATH_PROMPTS = {
    "drag_similar": "path_drag_similar.md",
    "drag_shadow": "path_drag_shadow.md",
    "drag_pairs": "path_drag_pairs.md",
    "drag_connection": "path_drag_connection.md",
}


class SpatialPathReasoner(SpatialReasoner[ImageDragDropChallenge]):
    """
    Spatial path reasoning tool for drag and drop challenges.

    Analyzes images to identify the correct drag-and-drop paths based on
    visual patterns and implicit matching rules.

    Attributes:
        description: The system prompt for the tool.
    """

    description: str = load_desc(Path(__file__).parent / "path.md")

    @classmethod
    def get_prompt_for_type(cls, challenge_type: str) -> str:
        """
        Get the appropriate prompt for a specific challenge type.
        
        Args:
            challenge_type: The type of drag challenge (e.g., 'drag_similar', 'drag_shadow')
            
        Returns:
            The prompt content for the specified challenge type, or default prompt if not found.
        """
        if challenge_type in PATH_PROMPTS:
            prompt_path = Path(__file__).parent / PATH_PROMPTS[challenge_type]
            if prompt_path.exists():
                return load_desc(prompt_path)
        # Fallback to default prompt
        return cls.description

    async def __call__(
        self,
        *,
        challenge_screenshot: Union[str, Path],
        grid_divisions: Union[str, Path],
        auxiliary_information: str | None = None,
        challenge_type: Optional[str] = None,
        **kwargs,
    ) -> ImageDragDropChallenge:
        """
        Analyze a drag-and-drop challenge and return the solution paths.

        Args:
            challenge_screenshot: Path to the challenge image.
            grid_divisions: Path to the grid overlay image.
            auxiliary_information: Optional challenge prompt or context.
            challenge_type: Optional specific drag type ('drag_similar', 'drag_shadow', 'drag_pairs', 'drag_connection')
            thinking_level: Thinking level for the model (default: HIGH).
            **kwargs: Additional options passed to the provider.

        Returns:
            ImageDragDropChallenge containing the drag paths.
        """
        # Use specific prompt if challenge_type is provided
        description = self.description
        if challenge_type and challenge_type in PATH_PROMPTS:
            description = self.get_prompt_for_type(challenge_type)
        
        return await self._invoke_spatial(
            challenge_screenshot=Path(challenge_screenshot),
            grid_divisions=Path(grid_divisions),
            auxiliary_information=auxiliary_information,
            response_schema=ImageDragDropChallenge,
            description=description,
            **kwargs,
        )
