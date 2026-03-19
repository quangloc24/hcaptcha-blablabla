import asyncio
import time
import random
import os
import re
from pathlib import Path
from loguru import logger
from typing import Union, Tuple, Optional
from contextlib import suppress
from playwright.async_api import Page, Frame, Locator, expect, FrameLocator
from hcaptcha_challenger.models import ChallengeTypeEnum, RequestType
from hcaptcha_challenger.agent.logger import LoggerHelper, log_method_call, ChallengeTracker

class PilotChallenges:
    def __init__(self, arm):
        self.arm = arm
        self.tracker = ChallengeTracker()

    def get_tracker(self):
        return self.tracker

    async def _wait_for_render_stability(self, frame: FrameLocator | Frame, max_wait_ms: int = 2000):
        """Phase 4: Ensures challenge view is not animating before capture."""
        try:
            challenge_view = frame.locator("//div[@class='challenge-view']")
            start_time = time.time()
            
            while (time.time() - start_time) * 1000 < max_wait_ms:
                # Capture two tiny fragments to check for pixel diff
                bbox = await challenge_view.bounding_box()
                if not bbox: break
                
                # Check for stability by comparing screenshots is slow, 
                # instead wait for loading indicator to be TRULY gone and add a buffer
                await self._wait_for_all_loaders_complete(frame)
                await asyncio.sleep(0.5) # Stability buffer
                return True
        except: pass
        return True

    async def _score_candidates(self, primary: list, alternatives: list[list], challenge_type: str, bbox: dict) -> list:
        """Phase 2: Heuristic Scorer to pick the best path sequence."""
        all_candidates = [primary] + alternatives
        best_sequence = primary
        best_score = -1.0
        
        for seq in all_candidates:
            if not seq: continue
            score = 0.0
            
            for path in seq:
                # 1. Canvas Bounds Check
                in_bounds = (
                    bbox['x'] <= path.start_point.x <= bbox['x'] + bbox['width'] and
                    bbox['y'] <= path.start_point.y <= bbox['y'] + bbox['height'] and
                    bbox['x'] <= path.end_point.x <= bbox['x'] + bbox['width'] and
                    bbox['y'] <= path.end_point.y <= bbox['y'] + bbox['height']
                )
                if in_bounds: score += 0.5
                
                # 2. Directional Check (Right to Left)
                if path.start_point.x > path.end_point.x: score += 0.3
                
                # 3. Confidence Weight
                score += (path.confidence * 0.2)
            
            # 4. Road Continuity Bonus
            if challenge_type == "drag_road" and len(seq) > 1:
                # Check if pieces are roughly on the same row or follow a path
                score += 0.1 

            if score > best_score:
                best_score = score
                best_sequence = seq
                
        return best_sequence

    async def _wait_for_all_loaders_complete(self, frame: Frame):
        """Implementation of lines 240-260 of the original: Ensures challenge images are loaded."""
        await asyncio.sleep(self.arm.config.WAIT_FOR_CHALLENGE_VIEW_TO_RENDER_MS / 1000)
        loading_indicators = frame.locator("//div[@class='loading-indicator']")
        count = await loading_indicators.count()
        if count == 0: return True
        
        for i in range(count):
            try:
                await expect(loading_indicators.nth(i)).to_have_attribute("style", re.compile(r"opacity:\s*0"), timeout=30000)
            except: pass
        return True

    async def _capture_burst_frames(self, frame: FrameLocator | Frame, cache_key: Path, cid: int, count: int = 5) -> list[Path]:
        """
        Captures a sequence of screenshots for motion challenges.
        """
        screenshots = []
        challenge_view = frame.locator("//div[@class='challenge-view']")
        
        for i in range(count):
            path = cache_key.joinpath(f"{cache_key.name}_{cid}_burst_{i}.png")
            path.parent.mkdir(parents=True, exist_ok=True)
            await challenge_view.screenshot(type="png", path=path, timeout=5000)
            screenshots.append(path)
            if i < count - 1:
                await asyncio.sleep(0.2) # 200ms between frames
        
        return screenshots

    async def _capture_spatial_mapping(self, frame: Frame, cache_key: Path, cid: Union[int, str]) -> Tuple[Optional[Path], Optional[Path]]:
        """Robust implementation of lines 270-340: Captures screenshot with MutationObserver and Canvas support."""
        await frame.evaluate("""
            async () => {
                const imgs = Array.from(document.querySelectorAll(".challenge-view img"));
                const canvas = Array.from(document.querySelectorAll(".challenge-view canvas"));
                const promises = [];
                imgs.forEach(img => {
                    if (!(img.complete && img.naturalWidth > 0)) {
                        promises.push(new Promise(resolve => {
                            img.onload = resolve;
                            img.onerror = resolve;
                        }));
                    }
                });
                canvas.forEach(c => {
                    if (!(c.width > 0 && c.height > 0)) {
                        promises.push(new Promise(resolve => {
                            const observer = new MutationObserver(() => {
                                if (c.width > 0 && c.height > 0) {
                                    observer.disconnect();
                                    resolve();
                                }
                            });
                            observer.observe(c, { attributes: true, attributeFilter: ['width', 'height'] });
                            setTimeout(() => { observer.disconnect(); resolve(); }, 2000);
                        }));
                    }
                });
                if (promises.length > 0) await Promise.all(promises);
                await new Promise(r => setTimeout(r, 400));
            }
        """)
        
        challenge_view = frame.locator("//div[@class='challenge-view']")
        bbox = await challenge_view.bounding_box()
        self.arm.navigation.current_view_bbox = bbox

        screenshot_path = cache_key.joinpath(f"{cache_key.name}_{cid}_challenge_view.png")
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        await challenge_view.screenshot(type="png", path=screenshot_path, timeout=5000)

        from hcaptcha_challenger.helper import create_coordinate_grid
        import matplotlib.pyplot as plt
        grid_img = create_coordinate_grid(
            screenshot_path, bbox,
            x_line_space_num=self.arm.config.coordinate_grid.x_line_space_num,
            y_line_space_num=self.arm.config.coordinate_grid.y_line_space_num,
            color=self.arm.config.coordinate_grid.color,
            adaptive_contrast=self.arm.config.coordinate_grid.adaptive_contrast,
        )

        grid_path = cache_key.joinpath(f"{cache_key.name}_{cid}_spatial_helper.png")
        plt.imsave(str(grid_path.resolve()), grid_img)

        return screenshot_path, grid_path

    async def _save_audit_artifacts(
        self, 
        category: str, 
        subtype: str, 
        raw_img: Union[Path, list[Path]], 
        grid_img: Optional[Path], 
        response: any, 
        cid: int,
        post_action_img: Optional[Path] = None
    ):
        """Phase 0: Saves challenge state to a structured audit folder."""
        if not self.arm.config.DEBUG_MODE:
            return
            
        try:
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            audit_dir = self.arm.config.audit_dir.joinpath(category, subtype, timestamp)
            audit_dir.mkdir(parents=True, exist_ok=True)
            
            # 1. Save images
            if isinstance(raw_img, list) and len(raw_img) > 0:
                # Burst mode: Generate GIF
                try:
                    from PIL import Image
                    frames = [Image.open(p) for p in raw_img if p.exists()]
                    if frames:
                        frames[0].save(
                            audit_dir / "original_burst.gif",
                            save_all=True,
                            append_images=frames[1:],
                            duration=200, loop=0
                        )
                except Exception as e:
                    logger.warning(f"Failed to save burst GIF: {e}")
            elif raw_img and raw_img.exists():
                from shutil import copy2
                copy2(raw_img, audit_dir / "original.png")
            
            if grid_img and grid_img.exists():
                from shutil import copy2
                copy2(grid_img, audit_dir / "grid_overlay.png")
                
            if post_action_img and post_action_img.exists():
                from shutil import copy2
                copy2(post_action_img, audit_dir / "post_action.png")
                
            # 2. Save JSON response
            import json
            from pydantic import BaseModel
            
            resp_data = response.dict() if isinstance(response, BaseModel) else str(response)
            with open(audit_dir / "ai_response.json", "w", encoding="utf-8") as f:
                json.dump(resp_data, f, indent=4, ensure_ascii=False)
                
            logger.info(f"Audit artifacts saved to {audit_dir}", emoji='📁')
        except Exception as e:
            logger.warning(f"Failed to save audit artifacts: {e}")

    async def _click_submit(self, frame):
        """Implementation of original line 980: Clicks submit button with human simulation."""
        selectors = [
            "//div[@class='button-submit button']",
            "//div[contains(@class, 'button-submit')]",
            "text=Submit",
            "text=Verify",
            "button:has-text('Verify')"
        ]
        
        for selector in selectors:
            try:
                btn = frame.locator(selector).first
                if await btn.is_visible(timeout=1000):
                    await asyncio.sleep(random.uniform(0.7, 1.4))
                    await self.arm.actions.click_by_mouse(btn)
                    
                    await asyncio.sleep(2)
                    with suppress(Exception):
                        error_locator = frame.locator("//div[contains(@class, 'error-text')]")
                        if await error_locator.is_visible(timeout=1000):
                            LoggerHelper.log_error("hCaptcha rejected solution!", emoji='boom')
                            return False
                    
                    LoggerHelper.log_info("Action sent. Waiting for verdict...", emoji='hourglass')
                    return True
            except: continue
        return False

    @log_method_call(emoji='🧩', color='magenta')
    async def handle_drag_drop(self, job_type: ChallengeTypeEnum):
        frame = await self.arm.navigation.get_challenge_frame_locator()
        if not frame: return False
        
        cache_key = self.arm.config.create_cache_key(self.arm.captcha_payload)
        user_prompt = self.arm._match_user_prompt(job_type)
        self.tracker.start_challenge(user_prompt)

        for cid in range(self.arm.crumb_count):
            round_start = time.time()
            LoggerHelper.log_round_start(cid + 1, self.arm.crumb_count)
            await self._wait_for_render_stability(frame)
            await self._wait_for_all_loaders_complete(frame)
            
            # Determine challenge type immediately for routing and audit
            raw_prompt = self.arm.captcha_payload.get_requester_question() if self.arm.captcha_payload else ""
            challenge_type = self._detect_drag_challenge_type(raw_prompt)
            
            raw, projection = await self._capture_spatial_mapping(frame, cache_key, cid)
            
            img_hash = self.arm.core.image_cache.get_hash(raw)
            if img_hash and img_hash in self.arm.core.image_cache.cache:
                LoggerHelper.log_info("Using Cache (HIT 🎯)", emoji='kermit')
                response = self.arm.core.image_cache.cache[img_hash]
                ai_duration = 0
                model_used = "cache"
            else:
                # Phase 3: Difficulty Routing
                # Select a stronger model for complex challenges like "Road Reconstruction"
                preferred_model = self.arm.config.SPATIAL_PATH_REASONER_MODEL
                if challenge_type == "drag_road":
                    LoggerHelper.log_info("Hard challenge detected. Requesting high-precision reasoning...", emoji='🧠')
                
                model, available_keys = await self.arm._get_available_model_and_keys(
                    preferred_model=preferred_model
                )
                
                # Log which prompt is being used
                if challenge_type:
                    LoggerHelper.log_info(f"Using specialized prompt: {challenge_type}", emoji='📝')
                else:
                    LoggerHelper.log_info("Using default global prompt", emoji='📝')
                
                # Soul Alignment: Dynamic Hinting based on challenge type
                # We inject the core strategy directly into the user hint to ensure priority
                type_hint = ""
                if challenge_type == "drag_road":
                    type_hint = (
                        "ROAD RECONSTRUCTION STRATEGY:\n"
                        "- SEQUENTIAL LOGIC: Draggable pieces are numbered. Connect them to their numeric neighbors on the grid (e.g., Piece 3 connects to Segment 2).\n"
                        "- VERTICAL SEAMING: For height transitions, target the geometric mid-point between row levels.\n"
                        "- ZERO OVERLAP: Each draggable piece must occupy a unique empty grid slot."
                    )
                elif challenge_type == "drag_connection":
                    type_hint = (
                        "CONNECTION TRACING STRATEGY:\n"
                        "- COLOR PATHS: Follow the specific colored line from the piece to its target.\n"
                        "- IGNORE INTERSECTIONS: Use color, not spatial overlap, to stay on the correct path.\n"
                        "- TERMINAL MATCH: The path must end at a semantic target (tree, building) of the same color."
                    )
                elif challenge_type == "drag_halves":
                    type_hint = (
                        "GEOMETRIC MATCHING STRATEGY:\n"
                        "- COMPLEMENTARY SHAPES: Match concave notches with convex bumps.\n"
                        "- PATTERN ALIGNMENT: Internal textures/lines must align perfectly.\n"
                        "- CELL CENTERING: Target the absolute center of the target grid cell."
                    )
                elif challenge_type == "drag_fit":
                    type_hint = (
                        "FITTING STRATEGY:\n"
                        "- SINGULAR TASK: This is usually one element. Verify if only ONE 'Move' handle exists.\n"
                        "- SILHOUETTE MATCH: Match the draggable element's outline to the empty contour on the canvas.\n"
                        "- CENTER ALIGNMENT: Drag to the geometric center of the fitting slot."
                    )
                elif challenge_type == "drag_pairs":
                    type_hint = (
                        "PAIR MATCHING STRATEGY:\n"
                        "- SIMILARITY: Match identical or semantically related items (e.g., A to A).\n"
                        "- HANDLE COUNT: Count 'Move' labels on the right. If 1 handle, return 1 path. If 4 handles, return 4 paths.\n"
                        "- PRECISE DROP: Align the center of the piece with the center of its match."
                    )

                # Calculate grid context for high-precision hints
                grid_config = self.arm.config.coordinate_grid
                bbox = self.arm.navigation.current_view_bbox
                x_step = bbox['width'] / (grid_config.x_line_space_num - 1)
                y_step = bbox['height'] / (grid_config.y_line_space_num - 1)
                
                grid_context = (
                    f"CANVAS BOUNDS: X[{int(bbox['x'])}-{int(bbox['x'] + bbox['width'])}], "
                    f"Y[{int(bbox['y'])}-{int(bbox['y'] + bbox['height'])}]\n"
                    f"GRID PRECISION: Major lines every {x_step:.1f}px (X) and {y_step:.1f}px (Y)."
                )

                ai_hint = (
                    f"{user_prompt}\n"
                    f"{type_hint}\n"
                    f"{grid_context}\n"
                    "INVENTORY LOCKDOWN: Count handles on the RIGHT. Return EXACTLY that many paths. Numbered circles ARE draggable pieces.\n"
                    "NO UI ELEMENTS: Ignore 'Move' text strips and grid axis numbers as candidates.\n"
                    "VALIDATION: start_point.x MUST be > end_point.x."
                )
                        
                
                try:
                    start_ai = time.time()
                    response = await self.arm.spatial_path_reasoner(
                        challenge_screenshot=raw,
                        grid_divisions=projection,
                        auxiliary_information=ai_hint,
                        challenge_type=challenge_type
                    )
                    ai_duration = time.time() - start_ai
                    model_used = model
                    if img_hash: self.arm.core.image_cache.cache[img_hash] = response
                    
                    # Quota feedback: Success
                    if model and available_keys:
                        k0 = available_keys[0]
                        self.arm.core.quota_manager.mark_success(k0, model)
                except Exception as e:
                    # Quota feedback: Error (Premium Portability)
                    total_keys = len(self.arm.config.GEMINI_API_KEYS) if self.arm.config.GEMINI_API_KEYS else 1
                    current_key_idx = 1
                    
                    if model and available_keys:
                        k0 = available_keys[0]
                        err_msg = str(e).lower()
                        if "429" in err_msg or "exhausted" in err_msg:
                            self.arm.core.quota_manager.mark_exhausted(k0, model)
                        else:
                            self.arm.core.quota_manager.mark_failure(k0, model)
                        
                        # Try to find real index in original config
                        for i, key_obj in enumerate(self.arm.config.GEMINI_API_KEYS):
                            val = key_obj.get_secret_value() if hasattr(key_obj, "get_secret_value") else str(key_obj)
                            if val == k0:
                                current_key_idx = i + 1
                                break
                    
                    self.arm.log_provider_error(current_key_idx, total_keys, e)
                    raise e

            self.arm._log_ai_response(response, cid + 1, self.arm.crumb_count)
            LoggerHelper.log_ai_performance(model_used, ai_duration, len(response.paths))
            self.arm.metrics.log_ai_call(ai_duration)

            # Phase 2: Heuristic Scorer (Top-K Selection)
            # Ensure challenge_type is at least None for the scorer
            best_paths = await self._score_candidates(
                primary=response.paths,
                alternatives=getattr(response, 'alternatives', []),
                challenge_type=challenge_type or "unknown",
                bbox=self.arm.navigation.current_view_bbox
            )

            for path in best_paths:
                # Validation: fix inverted coordinates (FROM should be right, TO left)
                if path.start_point.x < path.end_point.x:
                    path.start_point, path.end_point = path.end_point, path.start_point
                
                if self.arm._validate_coordinate(path.start_point.x, path.start_point.y):
                    LoggerHelper.log_info(f"DRAG: ({int(path.start_point.x)}, {int(path.start_point.y)}) -> ({int(path.end_point.x)}, {int(path.end_point.y)}) [Conf: {path.confidence:.2f}]", emoji='drag')
                    await self.arm.actions.perform_drag_drop(path, delay_ms=random.randint(15, 25))
                    await asyncio.sleep(random.uniform(0.5, 0.8))

            # Capture Post-Action Screenshot
            post_action_img = cache_key.joinpath(f"{cache_key.name}_{cid}_post_action.png")
            try:
                post_action_img.parent.mkdir(parents=True, exist_ok=True)
                challenge_view = frame.locator("//div[@class='challenge-view']")
                await challenge_view.screenshot(type="png", path=post_action_img, timeout=5000)
            except Exception as e:
                LoggerHelper.log_debug(f"Failed to capture post-action screenshot: {e}")
                post_action_img = None

            # Phase 0: Save Audit Artifacts
            await self._save_audit_artifacts(
                category="drag_drop",
                subtype=challenge_type or "unknown",
                raw_img=raw,
                grid_img=projection,
                response=response,
                cid=cid,
                post_action_img=post_action_img
            )

            is_success = await self._click_submit(frame)
            
            # Save final verdict
            audit_dir = self.arm.config.audit_dir.joinpath("drag_drop", challenge_type or "unknown")
            # Find the latest subdir (the one we just created)
            subdirs = sorted([d for d in audit_dir.iterdir() if d.is_dir()], key=os.path.getmtime)
            if subdirs:
                latest = subdirs[-1]
                import json
                with open(latest / "final_verdict.json", "w") as f:
                    json.dump({"verdict": "SUCCESS" if is_success else "FAIL"}, f)

            self.tracker.log_round(cid+1, is_success, time.time()-round_start, ai_duration, len(best_paths))

    def _detect_drag_challenge_type(self, user_prompt: str) -> str | None:
        """
        Detect the specific drag challenge type from the user prompt.
        Optimized for Soul Alignment (Penguins, Roads, Halves, Connections).
        """
        prompt_lower = user_prompt.lower() if user_prompt else ""
        
        # 1. drag_road: The "Penguin" / Road Completion case
        # Keywords: complete the line, segments, road, path
        if any(k in prompt_lower for k in ["line", "road", "segment", "path"]):
            LoggerHelper.log_info("Detected drag type: drag_road (Road Reconstruction)", emoji='🐧')
            return "drag_road"
        
        # 2. drag_connection: Trace the Circuit / Color paths
        if any(k in prompt_lower for k in ["connect", "tree", "circuit"]):
            LoggerHelper.log_info("Detected drag type: drag_connection (Color-based connection)", emoji='🎯')
            return "drag_connection"
        
        # 3. drag_halves: Complementary Shapes / Completing the unit
        if any(k in prompt_lower for k in ["half", "shape", "complete the", "complementary"]):
            LoggerHelper.log_info("Detected drag type: drag_halves (Geometric Matching)", emoji='🧩')
            return "drag_halves"
        
        # 4. drag_shadow: Shadow matching
        if "shadow" in prompt_lower or "pattern that match" in prompt_lower:
            LoggerHelper.log_info("Detected drag type: drag_shadow (Shadow matching)", emoji='🌑')
            return "drag_shadow"
        
        # 5. drag_fit: Generic fitting
        if "fits" in prompt_lower or "place where it fit" in prompt_lower:
            LoggerHelper.log_info("Detected drag type: drag_fit (Fitting logic)", emoji='📦')
            return "drag_fit"
        
        # 6. drag_pairs: Pair/Similarity matching
        if "pair" in prompt_lower or "letter" in prompt_lower or "match" in prompt_lower or "similar" in prompt_lower:
            LoggerHelper.log_info("Detected drag type: drag_pairs (Pair matching)", emoji='👫')
            return "drag_pairs"
        
        # No specific type detected
        LoggerHelper.log_info("Using default simplified drag prompt", emoji='📋')
        return None

    @log_method_call(emoji='🎯', color='cyan')
    async def handle_label_select(self, job_type: ChallengeTypeEnum):
        frame = await self.arm.navigation.get_challenge_frame_locator()
        if not frame: return False
        
        cache_key = self.arm.config.create_cache_key(self.arm.captcha_payload)
        user_prompt = self.arm._match_user_prompt(job_type)
        self.tracker.start_challenge(user_prompt)

        for cid in range(self.arm.crumb_count):
            round_start = time.time()
            LoggerHelper.log_round_start(cid + 1, self.arm.crumb_count)
            
            await self._wait_for_render_stability(frame)
            await self._wait_for_all_loaders_complete(frame)

            # Soul Alignment: Motion Pattern Detection -> Burst Mode 📸
            real_prompt = self.arm.captcha_payload.get_requester_question().lower() if self.arm.captcha_payload else ""
            logger.info(f"DEBUG: Checking motion. Real Prompt: '{real_prompt}'")
            
            motion_keywords = ["motion", "pattern", "move", "different", "fastest", "slowest", "differently"]
            is_motion = any(k in real_prompt for k in motion_keywords)
            logger.info(f"DEBUG: is_motion={is_motion}")
            
            # Ensure bbox is defined beforehand
            bbox = await frame.locator("//div[@class='challenge-view']").bounding_box()
            self.arm.navigation.current_view_bbox = bbox

            if is_motion:
                LoggerHelper.log_info("Motion Challenge detected! Activating Burst Mode (5 frames)...", emoji='📸')
                challenge_screenshots = await self._capture_burst_frames(frame, cache_key, cid, count=5)
                
                # For grid, we use the last frame of the burst (bbox already defined above)
                
                from hcaptcha_challenger.helper import create_coordinate_grid
                import matplotlib.pyplot as plt
                
                grid_result = create_coordinate_grid(
                    challenge_screenshots[-1], # Uses last frame for the grid
                    bbox,

                    x_line_space_num=self.arm.config.coordinate_grid.x_line_space_num,
                    y_line_space_num=self.arm.config.coordinate_grid.y_line_space_num,
                    color=self.arm.config.coordinate_grid.color,
                    adaptive_contrast=self.arm.config.coordinate_grid.adaptive_contrast,
                )
                projection = cache_key.joinpath(f"{cache_key.name}_{cid}_spatial_helper.png")
                projection.parent.mkdir(parents=True, exist_ok=True)
                plt.imsave(str(projection.resolve()), grid_result)
                
                raw = challenge_screenshots # Pass the LIST of paths
                LoggerHelper.log_info(f"Burst Mode completed: {len(raw)} frames captured.", emoji='🎞️')
            else:
                raw, projection = await self._capture_spatial_mapping(frame, cache_key, cid)
            
            model, available_keys = await self.arm._get_available_model_and_keys(
                preferred_model=self.arm.config.SPATIAL_POINT_REASONER_MODEL
            )
            
            # Calculate grid context for high-precision hints
            grid_config = self.arm.config.coordinate_grid
            bbox = self.arm.navigation.current_view_bbox
            x_step = bbox['width'] / (grid_config.x_line_space_num - 1)
            y_step = bbox['height'] / (grid_config.y_line_space_num - 1)
            
            grid_context = (
                f"CANVAS BOUNDS: X[{int(bbox['x'])}-{int(bbox['x'] + bbox['width'])}], "
                f"Y[{int(bbox['y'])}-{int(bbox['y'] + bbox['height'])}]\n"
                f"GRID PRECISION: Major lines every {x_step:.1f}px (X) and {y_step:.1f}px (Y)."
            )

            # Determine hint based on motion or uniqueness
            if is_motion:
                type_hint = (
                    "BURST MODE ANALYSIS (TEMPORAL DELTA):\n"
                    "- FRAME COMPARISON: Analyze pixel displacement between Frame 0 and Frame 4.\n"
                    "- ANOMALY DETECTION: Identify the object moving at a different velocity or rotational vector (CW vs CCW).\n"
                    "- BEHAVIORAL PATH: Ignore spatial separation; focus ONLY on behavioral inconsistency across frames."
                )
            else:
                type_hint = (
                    "UNIQUENESS STRATEGY:\n"
                    "- CHARACTERISTIC ANOMALY: Identify the object with a different color, shape, or texture than its peers.\n"
                    "- GRID LOCK: Align target center using X/Y labels."
                )

            ai_hint = (
                f"{user_prompt}\n"
                f"{type_hint}\n"
                f"{grid_context}\n"
                "COORDINATE ACCURACY: Use X/Y grid labels. Return exact center points.\n"
                "NO UI ELEMENTS: Ignore 'Verify' buttons or labels."
            )
            
            try:
                start_ai = time.time()
                response = await self.arm.spatial_point_reasoner(
                    challenge_screenshot=raw,
                    grid_divisions=projection,
                    auxiliary_information=ai_hint
                )
                ai_duration = time.time() - start_ai
                if model and available_keys:
                    k0 = available_keys[0]
                    self.arm.core.quota_manager.mark_success(k0, model)
            except Exception as e:
                # Feedback de quota: Erro (Portabilidade Premium)
                total_keys = len(self.arm.config.GEMINI_API_KEYS) if self.arm.config.GEMINI_API_KEYS else 1
                current_key_idx = 1
                
                if model and available_keys:
                    k0 = available_keys[0]
                    err_msg = str(e).lower()
                    if "429" in err_msg or "exhausted" in err_msg:
                        self.arm.core.quota_manager.mark_exhausted(k0, model)
                    else:
                        self.arm.core.quota_manager.mark_failure(k0, model)
                    
                    for i, key_obj in enumerate(self.arm.config.GEMINI_API_KEYS):
                        val = key_obj.get_secret_value() if hasattr(key_obj, "get_secret_value") else str(key_obj)
                        if val == k0:
                            current_key_idx = i + 1
                            break
                
                self.arm.log_provider_error(current_key_idx, total_keys, e)
                raise e
            
            self.arm._log_ai_response(response, cid + 1, self.arm.crumb_count)
            points = getattr(response, 'points', [])
            LoggerHelper.log_ai_performance(model, ai_duration, len(points))
            self.arm.metrics.log_ai_call(ai_duration)

            for point in points:
                # SOUL ALIGNMENT: SpatialPointReasoner returns GLOBAL coordinates
                # The grid_divisions image contains absolute coordinate labels
                # Therefore, we do NOT add offset here (as in line 674 of the original)
                await self.arm.page.mouse.click(point.x, point.y, delay=180)
                await asyncio.sleep(random.uniform(0.4, 0.6))

            # Capture Post-Action Screenshot
            post_action_img = cache_key.joinpath(f"{cache_key.name}_{cid}_post_action.png")
            try:
                post_action_img.parent.mkdir(parents=True, exist_ok=True)
                challenge_view = frame.locator("//div[@class='challenge-view']")
                await challenge_view.screenshot(type="png", path=post_action_img, timeout=5000)
            except Exception as e:
                LoggerHelper.log_debug(f"Failed to capture post-action screenshot: {e}")
                post_action_img = None

            # Phase 0: Save Audit Artifacts
            await self._save_audit_artifacts(
                category="label_select",
                subtype="motion" if is_motion else "point",
                raw_img=raw,
                grid_img=projection,
                response=response,
                cid=cid,
                post_action_img=post_action_img
            )

            await self._click_submit(frame)
            self.tracker.log_round(cid+1, True, time.time()-round_start, ai_duration, len(points))

    @log_method_call(emoji='🖼️', color='green')
    async def handle_binary(self):
        frame = await self.arm.navigation.get_challenge_frame_locator()
        if not frame: return False
        
        cache_key = self.arm.config.create_cache_key(self.arm.captcha_payload)
        user_prompt = self.arm._match_user_prompt(RequestType.IMAGE_LABEL_BINARY)
        self.tracker.start_challenge(user_prompt)

        for cid in range(self.arm.crumb_count):
            round_start = time.time()
            LoggerHelper.log_round_start(cid + 1, self.arm.crumb_count)
            await self._wait_for_render_stability(frame)
            await self._wait_for_all_loaders_complete(frame)
            
            raw = await self.arm._get_challenge_image(frame, cache_key, cid)
            model, available_keys = await self.arm._get_available_model_and_keys(
                preferred_model=self.arm.config.IMAGE_CLASSIFIER_MODEL
            )
            
            try:
                start_ai = time.time()
                response = await self.arm.image_classifier(
                    challenge_screenshot=raw, 
                    auxiliary_information=user_prompt
                )
                ai_duration = time.time() - start_ai
                if model and available_keys:
                    k0 = available_keys[0]
                    self.arm.core.quota_manager.mark_success(k0, model)
            except Exception as e:
                # Feedback de quota: Erro (Portabilidade Premium)
                total_keys = len(self.arm.config.GEMINI_API_KEYS) if self.arm.config.GEMINI_API_KEYS else 1
                current_key_idx = 1
                
                if model and available_keys:
                    k0 = available_keys[0]
                    err_msg = str(e).lower()
                    if "429" in err_msg or "exhausted" in err_msg:
                        self.arm.core.quota_manager.mark_exhausted(k0, model)
                    else:
                        self.arm.core.quota_manager.mark_failure(k0, model)
                    
                    for i, key_obj in enumerate(self.arm.config.GEMINI_API_KEYS):
                        val = key_obj.get_secret_value() if hasattr(key_obj, "get_secret_value") else str(key_obj)
                        if val == k0:
                            current_key_idx = i + 1
                            break
                
                self.arm.log_provider_error(current_key_idx, total_keys, e)
                raise e
            
            matrix = response.convert_box_to_boolean_matrix()
            LoggerHelper.log_ai_performance(model, ai_duration, sum(matrix))
            self.arm.metrics.log_ai_call(ai_duration)
            
            for i, should_click in enumerate(matrix):
                if should_click:
                    selector = f"//div[@class='task' and contains(@aria-label, '{i+1}')]"
                    await self.arm.actions.click_by_mouse(frame.locator(selector))
            
            # Capture Post-Action Screenshot
            post_action_img = cache_key.joinpath(f"{cache_key.name}_{cid}_post_action.png")
            try:
                post_action_img.parent.mkdir(parents=True, exist_ok=True)
                challenge_view = frame.locator("//div[@class='challenge-view']")
                await challenge_view.screenshot(type="png", path=post_action_img, timeout=5000)
            except Exception as e:
                LoggerHelper.log_debug(f"Failed to capture post-action screenshot: {e}")
                post_action_img = None

            # Phase 0: Save Audit Artifacts
            await self._save_audit_artifacts(
                category="binary",
                subtype="grid_image",
                raw_img=raw,
                grid_img=None,
                response=response,
                cid=cid,
                post_action_img=post_action_img
            )

            await self._click_submit(frame)
            self.tracker.log_round(cid+1, True, time.time()-round_start, ai_duration, sum(matrix))


    async def debug_find_captcha(self):
        """
        Implementation of original line 345: Tries to find captcha components for debugging.
        "Swiss Army Knife" system for detection and activation.
        """
        page = self.arm.page
        LoggerHelper.log_info("Looking for captcha on page...", emoji='🔍')
        
        # 1. Try to find the Cockpit (Challenge Frame) directly first
        frame = await self.arm.navigation.get_challenge_frame_locator()
        if frame:
            LoggerHelper.log_success(f"Cockpit found: {frame.url[:60]}...", emoji='🎯')
            return True
            
        # 2. Exhaustive iframe scan (Parity Restoration)
        iframes = await page.query_selector_all('iframe')
        LoggerHelper.log_info(f"Total iframes detected: {len(iframes)}", emoji='📋')
        
        for i, iframe in enumerate(iframes):
            try:
                src = await iframe.get_attribute('src') or ''
                if 'hcaptcha' in src.lower() or 'captcha' in src.lower():
                    LoggerHelper.log_success(f"Iframe {i} POSSIBLE CAPTCHA: {src[:60]}...", emoji='🎯')
                    
                    content_frame = await iframe.content_frame()
                    if content_frame:
                        # Check Checkbox
                        checkbox = content_frame.locator('div#checkbox')
                        if await checkbox.is_visible(timeout=1000):
                            LoggerHelper.log_success("hCaptcha Checkbox visible! Clicking...", emoji='✅')
                            await self.arm.actions.click_by_mouse(checkbox)
                            await asyncio.sleep(2) # Wait for transition
                            return True
            except:
                continue

        # 3. Check for data selectors (data-sitekey, etc)
        selectors = [
            'div[class*="h-captcha"]', 'div[class*="hcaptcha"]', 
            'div[data-sitekey]', 'div#h-captcha', '.h-captcha'
        ]
        for selector in selectors:
            if await page.locator(selector).count() > 0:
                LoggerHelper.log_success(f"Captcha element detected via selector: {selector}", emoji='🎯')
                # Try to click checkbox if found
                try: 
                    await self.arm.actions.click_checkbox() 
                    return True
                except: pass
                return True

        LoggerHelper.log_warning("Cockpit not located.")
        return False
