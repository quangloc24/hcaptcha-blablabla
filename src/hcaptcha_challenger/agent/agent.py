# -*- coding: utf-8 -*-
import asyncio
import time
from typing import List
from contextlib import suppress
from playwright.async_api import Page

from hcaptcha_challenger.models import ChallengeSignal, ChallengeTypeEnum, RequestType
from hcaptcha_challenger.agent.config import AgentConfig, SolveState
from hcaptcha_challenger.agent.logger import LoggerHelper, log_captcha_payload, log_method_call
from .robotic_arm import RoboticArm

class AgentV:
    """
    Main agent that manages the full hCaptcha resolving flow.
    State system ported from lines 1260-1362 of the original.
    """
    def __init__(self, page: Page, agent_config: AgentConfig):
        self.page = page
        self.config = agent_config
        self.arm = RoboticArm(page, agent_config)
        self.core = self.arm.core  # Shortcut
        self.metrics = self.arm.metrics
        
        # Sistema de estados (portado do original)
        self.state = SolveState.INIT
        self.reset_count = 0
        self.challenge_attempts = 0
        self.last_payload = None
        
        # Lista de respostas válidas
        self.cr_list: List = []

    async def _check_ignore_list(self):
        """Checks if the challenge is in the ignore list (Python 3.8+ compatible)."""
        if self.config.ignore_request_questions and self.arm.captcha_payload:
            for q in self.config.ignore_request_questions:
                if q in self.arm.captcha_payload.get_requester_question():
                    await asyncio.sleep(2)
                    await self.arm.navigation.refresh_challenge()
                    return False
        return True

    @log_method_call(emoji='🚀', color='green')
    async def wait_for_challenge(self) -> ChallengeSignal:
        """
        Main method - Resolution loop with state system.
        Ported from lines 1260-1362 of the original.
        """
        # FULL STATE RESET
        self.state = SolveState.INIT
        self.reset_count = 0
        self.challenge_attempts = 0
        self.last_payload = None
        
        # Soul Alignment: Reset robotic arm payload to avoid leakage between sessions
        self.arm.captcha_payload = None
        
        # Clear queues (Premium Portability: Ensures no radioactive waste in the pipeline)
        while not self.core.captcha_response_queue.empty():
            try: self.core.captcha_response_queue.get_nowait()
            except: break
            
        while not self.core.captcha_payload_queue.empty():
            try: self.core.captcha_payload_queue.get_nowait()
            except: break
        
        start_time_total = time.time()
        LoggerHelper.log_info("🚀 STARTING NEW SESSION (State reset)", emoji='🔄')
        
        LoggerHelper.log_info("Checking page state...", emoji='🔍')
        # Try to find captcha physically
        found = await self.arm.challenges.debug_find_captcha()
        if not found:
            LoggerHelper.log_error("Bypass impossible: Captcha not detected!", emoji='skull')
            return ChallengeSignal.FAILURE
        
        while self.state not in [SolveState.SUCCESS, SolveState.FAILURE]:
            self.challenge_attempts += 1
            if self.challenge_attempts > self.config.MAX_CHALLENGE_ATTEMPTS:
                LoggerHelper.log_error(f"Attempt limit reached ({self.config.MAX_CHALLENGE_ATTEMPTS})", emoji='boom')
                self.state = SolveState.FAILURE
                break

            if self.state in [SolveState.INIT, SolveState.CHALLENGE_PENDING]:
                # Clear response queue
                while not self.core.captcha_response_queue.empty():
                    self.core.captcha_response_queue.get_nowait()
                
                # Payload log if available (Soul Alignment)
                if self.arm.captcha_payload:
                    log_captcha_payload(self.arm.captcha_payload)
                
                # Solve the captcha
                try:
                    timeout_seconds = 120
                    result = await asyncio.wait_for(self._solve_captcha_flow(), timeout=timeout_seconds)
                    if result:
                        self.state = SolveState.SUBMITTED
                    else:
                        self.reset_count += 1
                        if self.reset_count > self.config.MAX_RESETS:
                            LoggerHelper.log_error(f"Reset limit reached ({self.config.MAX_RESETS})", emoji='boom')
                            self.state = SolveState.FAILURE
                            continue
                        
                        LoggerHelper.log_warning(f"Attempt {self.reset_count}/{self.config.MAX_RESETS} failed.", emoji='refresh')
                        await self.page.reload(wait_until="domcontentloaded", timeout=5000)
                        await asyncio.sleep(2)
                        try:
                            await self.arm.actions.click_checkbox()
                        except:
                            pass
                        self.state = SolveState.INIT
                        continue
                except asyncio.TimeoutError:
                    LoggerHelper.log_warning("Timeout during resolution. Restarting...", emoji='refresh')
                    await self.page.reload(wait_until="domcontentloaded", timeout=5000)
                    await asyncio.sleep(2)
                    self.state = SolveState.INIT
                    continue
                except Exception as err:
                    LoggerHelper.log_error(f"Critical error: {err}")
                    await self.page.reload(wait_until="domcontentloaded", timeout=10000)
                    await asyncio.sleep(2)
                    self.state = SolveState.INIT
                    continue
            
            if self.state == SolveState.SUBMITTED:
                LoggerHelper.log_info("Waiting for hCaptcha verdict...", emoji='hourglass')
                
                # Wait for response or new payload
                res_task = asyncio.create_task(self.core.captcha_response_queue.get())
                pay_task = asyncio.create_task(self.core.captcha_payload_queue.get())
                
                try:
                    done, pending = await asyncio.wait(
                        [res_task, pay_task], 
                        timeout=self.config.RESPONSE_TIMEOUT, 
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    
                    for t in pending: 
                        t.cancel()
                    
                    if not done:
                        self.state = SolveState.FAILURE
                        continue
                    
                    if pay_task in done:
                        # New payload received (another round)
                        self.reset_count += 1
                        if self.reset_count > self.config.MAX_RESETS:
                            self.state = SolveState.FAILURE
                        else:
                            self.arm.captcha_payload = pay_task.result()
                            self.state = SolveState.CHALLENGE_PENDING
                        continue
                    
                    if res_task in done:
                        cr = res_task.result()
                        if cr and cr.is_pass:
                            self.core.cache_validated_response(cr)
                            self.state = SolveState.SUCCESS
                        else:
                            if self.config.RETRY_ON_FAILURE:
                                await asyncio.sleep(2)
                                self.state = SolveState.INIT
                            else:
                                self.state = SolveState.FAILURE
                
                except Exception:
                    self.state = SolveState.FAILURE
        
        duration = time.time() - start_time_total
        
        if self.state == SolveState.SUCCESS:
            # Get success token
            token = await self.page.evaluate(
                "() => document.querySelector('[name=\"h-captcha-response\"]')?.value"
            )
            if token:
                LoggerHelper.log_success(f"Bypass confirmed! Token: {token[:24]}...", emoji='key')
            
            LoggerHelper.log_success("MISSION SUCCESSFULLY COMPLETED!", emoji='trophy')
            self.arm.challenges.get_tracker().print_summary()
            self.metrics.log_challenge_result(True, duration)
            self.metrics.print_summary()
            return ChallengeSignal.SUCCESS
        else:
            LoggerHelper.log_error("MISSION FAILED", emoji='skull')
            self.arm.log_failure_summary(
                duration=duration, 
                error="Multiple failures or timeout", 
                retry_count=self.challenge_attempts, 
                total_retries=self.config.MAX_CHALLENGE_ATTEMPTS
            )
            self.arm.challenges.get_tracker().print_summary()
            self.metrics.log_challenge_result(False, duration)
            self.metrics.print_summary()
            return ChallengeSignal.FAILURE

    async def _solve_captcha_flow(self):
        """
        Main resolution flow.
        Combines review_challenge_type and solve_captcha from original.
        """
        start_time = time.time()
        # 1. Determine challenge type
        challenge_type = await self.core.review_challenge_type()
        if not challenge_type:
            LoggerHelper.log_warning("Could not determine challenge type")
            return False
        
        self.state = SolveState.CHALLENGING
        
        # 2. Log challenge start
        if self.arm.captcha_payload:
            prompt = self.arm.captcha_payload.get_requester_question()
            self.arm.challenges.get_tracker().start_challenge(prompt)
            
            LoggerHelper.log_challenge_start(
                challenge_type.value if hasattr(challenge_type, 'value') else str(challenge_type),
                self.reset_count + 1,
                self.config.MAX_RESETS,
                prompt=prompt,
                timeout=120
            )
        
        # 3. Check ignore list
        # 3. Check ignore list (Python 3.8+ compatible)
        try:
            # Timeout de 5 segundos para verificação de ignore list
            should_continue = await asyncio.wait_for(
                self._check_ignore_list(),
                timeout=5.0
            )
            if not should_continue:
                return False
        except asyncio.TimeoutError:
            pass  # Ignore timeout, continue with flow
        
        # 4. Delegate to correct handler
        try:
            await self.core.solve_captcha(challenge_type)
            return True
        except Exception as err:
            duration = time.time() - start_time
            LoggerHelper.log_error(f"Flow interrupted ({duration:.1f}s): {err}")
            return False

    def _cache_validated_captcha_response(self, cr):
        """Compatibility with existing code."""
        self.core.cache_validated_response(cr)
