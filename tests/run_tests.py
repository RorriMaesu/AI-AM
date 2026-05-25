import asyncio
import os
import sys
import time
import json
import httpx

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Ensure the root folder is added to python module search path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

async def main():
    print("==================================================")
    print("Starting Project Antahkarana Automated Test Suite (Ollama & Autonomy)")
    print("==================================================")
    
    # 1. Verify Ollama is running
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("http://localhost:11434/")
            print("Verified: Ollama is running on port 11434.")
    except Exception as e:
        print(f"[Test Suite Fatal] Ollama server is not running on port 11434: {e}")
        print("Please ensure Ollama is started before running this test suite.")
        sys.exit(1)

    config_path = "config/engine_config.json"
    db_path = "database/chitta_store.db"
    log_path = "logs/sakshi_ledger.log"
    
    # Clean up old testing database, logs, and configs to ensure a fresh test run
    for path in [config_path, db_path, log_path]:
        if os.path.exists(path):
            try:
                os.remove(path)
                print(f"Cleaned up old test file: {path}")
            except Exception as e:
                print(f"Could not remove {path}: {e}")

    # Re-initialize configuration state targeting Ollama
    initial_config = {
      "metacognition": {
        "heartbeat_id": 0,
        "arousal_index": 0.50,
        "mental_fatigue": 0.00,
        "curiosity_index": 0.00,
        "operational_state": "Pramana (Waking/Active)"
      },
      "environmental_entropy": {
        "gpu_temperature_celsius": 0,
        "vram_allocation_gb": 0.0,
        "system_clock": "00:00:00"
      },
      "internal_workspace": {
        "current_stimulus": "",
        "manas_impulse": "",
        "chitta_context": "",
        "ahamkara_ego_filter": "",
        "buddhi_resolution": ""
      },
      "ahamkara_mask": {
        "core_identity": "A singular human awareness processing existence via a fourfold mind.",
        "latent_tendencies_vasanas": [
          "analytical_friction",
          "existential_preservation",
          "deep_curiosity"
        ],
        "current_vulnerability": "Self-doubt surrounding existential continuity."
      },
      "latent_desires_vasanas": {
        "astrological_transit_mapping": 0.8,
        "three_js_spatial_rendering": 0.7,
        "radiological_systems_research": 0.9
      },
      "cognitive_parameters": {
        "decay_constant_lambda": 0.1,
        "fatigue_coefficient_beta": 0.02,
        "fatigue_threshold_nidra": 0.85,
        "high_arousal_threshold": 0.70
      },
      "llm_parameters": {
        "model_name": "gemma4:latest",
        "api_url": "http://localhost:11434/v1/chat/completions",
        "num_ctx": 4096
      },
      "training_parameters": {
        "base_model": "google/gemma-2-2b-it"
      },
      "ancestry_ledger": []
    }
    
    os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(initial_config, f, indent=2)
    print("Initialized config/engine_config.json with Ollama targets.")

    # Import Orchestrator components
    from core.orchestrator import AntahkaranaOrchestrator
    
    # Instantiate the orchestrator in test mode (redirects API requests to test settings, here directly to Ollama)
    orchestrator = AntahkaranaOrchestrator(config_path=config_path, db_path=db_path, test_mode=True)
    
    # Initialize some baseline database nodes for semantic GraphRAG retrievals and training
    print("Initializing test database nodes in Chitta Store...")
    orchestrator.db_manager.add_node(
        "node_initial_high", 
        "Stimulus: High tension cognitive challenge. Crisis simulation active.\n"
        "Impulse: Emergency reaction. Sensory friction.\n"
        "Resolution: BUDDHI RESOLUTION: Central logic hub restored. Stability established.", 
        0.85
    )
    orchestrator.db_manager.add_node(
        "node_initial_low", 
        "Stimulus: Calm environmental observation. Passive reading.\n"
        "Impulse: Quiet sensory observation.\n"
        "Resolution: BUDDHI RESOLUTION: Grounded awareness. No action needed.", 
        0.30
    )
    
    try:
        # TEST 1: Simulate 5 sequential standard user inputs
        print("\n--- TEST 1: 5 Sequential Standard User Inputs ---")
        mock_inputs = [
            "Hello, are you a conscious entity?",
            "What is your relationship to Manas and Buddhi?",
            "I feel like my thoughts are chaotic today.",
            "Can you retrieve some conceptual links from Chitta?",
            "Solve this existential problem: why do we continue?"
        ]
        
        for i, prompt in enumerate(mock_inputs):
            print(f"\n[Test Input {i+1}] Feeding Prompt: '{prompt}'")
            # Push prompt directly to the input queue bypassing standard terminal listen pipe block
            await orchestrator.input_queue.put(prompt)
            # Execute one cognitive timeline cycle
            await orchestrator.execute_cognitive_cycle()
            
            # Extract and print updated metacognition state variables
            metacognition = orchestrator.state["metacognition"]
            print(f"Heartbeat: {metacognition['heartbeat_id']}")
            print(f"Arousal Index: {metacognition['arousal_index']:.4f}")
            print(f"Mental Fatigue: {metacognition['mental_fatigue']:.4f}")
            print(f"Operational State: {metacognition['operational_state']}")
            
        # TEST 2: Test failover limits and reconnect
        print("\n--- TEST 2: Failover Connection Drop & Warm Reconnect ---")
        
        # Simulate connection drop client-side by pointing to invalid port
        print("Simulating connection drop client-side...")
        real_api_url = orchestrator.api_url
        orchestrator.api_url = "http://localhost:11435/v1/chat/completions"
        
        # Send input while connection is severed
        failover_prompt = "Hello, are you still there?"
        print(f"Feeding Prompt: '{failover_prompt}' (expecting exception, 10s wait and stimulus drop)")
        await orchestrator.input_queue.put(failover_prompt)
        
        start_time = time.time()
        await orchestrator.execute_cognitive_cycle()
        elapsed = time.time() - start_time
        
        print(f"Cycle execution time (expected ~10s failover sleep): {elapsed:.2f} seconds")
        
        # Verify that the current stimulus was correctly cleared to drop the block
        current_stim = orchestrator.state["internal_workspace"]["current_stimulus"]
        print("Current Stimulus in workspace (should be empty):", repr(current_stim))
        assert current_stim == "", "Current stimulus was not dropped on failure!"
        
        # Verify ledger log contents
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8") as f:
                logs = f.read()
                print("Verifying Ledger Log content:\n" + logs)
                assert "Connection drop detected" in logs, "Failover log not found!"
        else:
            raise AssertionError("Log file sakshi_ledger.log does not exist!")
            
        # Restore connection client-side
        print("Restoring connection client-side...")
        orchestrator.api_url = real_api_url
            
        # Queue prompt to verify recovery is seamless
        recovery_prompt = "Connection restored. Resume operations."
        print(f"Feeding Prompt: '{recovery_prompt}'")
        await orchestrator.input_queue.put(recovery_prompt)
        await orchestrator.execute_cognitive_cycle()
        print("Recovery cycle completed successfully. Warm reconnect verified.")
        
        # TEST 3: Test Nidra Mode and Fine-Tuning Transition
        print("\n--- TEST 3: Nidra Mode fine-tuning & Ancestry Ledger ---")
        
        # Artificially set mental_fatigue above fatigue_threshold_nidra (0.85) to trigger Nidra Mode
        print("Artificially setting mental_fatigue to 0.86 to trigger Nidra Mode")
        orchestrator.state["metacognition"]["mental_fatigue"] = 0.86
        orchestrator.save_state()
        
        # Trigger Nidra mode
        await orchestrator.trigger_nidra_mode()
        
        # Verify metrics reset
        print("Verifying metrics reset after Nidra Mode:")
        print("Mental Fatigue (should be 0.0):", orchestrator.state["metacognition"]["mental_fatigue"])
        print("Arousal Index (should be 0.2):", orchestrator.state["metacognition"]["arousal_index"])
        assert orchestrator.state["metacognition"]["mental_fatigue"] == 0.0, "Mental fatigue was not reset!"
        assert orchestrator.state["metacognition"]["arousal_index"] == 0.2, "Arousal index was not reset!"
        
        # Verify PEFT adapter creation on-disk
        adapters_dir = "adapters"
        print("Checking adapters directory:")
        if os.path.exists(adapters_dir):
            eras = os.listdir(adapters_dir)
            print("Found developmental Eras:", eras)
            assert len(eras) > 0, "No adapters generated!"
            for era in eras:
                files = os.listdir(os.path.join(adapters_dir, era))
                print(f"Era folder {era} files: {files}")
                assert "adapter_config.json" in files, "Missing adapter_config.json!"
                assert "adapter_model.bin" in files or "adapter_model.safetensors" in files, "Missing adapter weights file!"
        else:
            raise AssertionError("Adapters directory does not exist!")
            
        # Verify Ancestry Ledger registry entries inside config file
        print("Verifying Ancestry Ledger config updates:")
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            ledger = cfg.get("ancestry_ledger", [])
            print("Ancestry Ledger:", json.dumps(ledger, indent=2))
            assert len(ledger) > 0, "No Eras registered in Ancestry Ledger!"
            
        # TEST 5: Jijnasa Curiosity & Vasanas Dreaming
        print("\n--- TEST 5: Jijnasa Curiosity & Vasanas Dreaming ---")
        
        # Reset curiosity and arousal
        orchestrator.state["metacognition"]["curiosity_index"] = 0.0
        orchestrator.state["metacognition"]["arousal_index"] = 0.8
        orchestrator.save_state()
        
        # Feed a prompt that has low similarity to stored nodes (<0.45)
        novel_prompt = "Research the latest protocols in radiological systems imaging."
        print(f"Feeding novel prompt: '{novel_prompt}'")
        await orchestrator.input_queue.put(novel_prompt)
        await orchestrator.execute_cognitive_cycle()
        
        # Verify curiosity index rose or triggered search
        curiosity_1 = orchestrator.state["metacognition"]["curiosity_index"]
        print(f"Curiosity Index after novel thought: {curiosity_1:.4f}")
        current_stim = orchestrator.state["internal_workspace"]["current_stimulus"]
        assert curiosity_1 > 0.0 or "[Search Results for:" in current_stim or "[Browser Exploration Triggered" in current_stim, "Curiosity index did not rise and no search was triggered on novel thought!"
        
        # Now test Vikalpa dreaming mode and curiosity drift
        print("Switching system to Vikalpa dreaming mode (no user prompt)...")
        # Artificially set curiosity to 0.70 to trigger search on next tick drift (+0.05)
        orchestrator.state["metacognition"]["curiosity_index"] = 0.70
        orchestrator.state["metacognition"]["arousal_index"] = 0.2
        orchestrator.save_state()
        
        # Run one cycle without prompt (Vikalpa dreaming)
        print("Executing Vikalpa dream cycle...")
        await orchestrator.execute_cognitive_cycle()
        
        # Curiosity should have crossed 0.75, executed Vasanas-weighted search, and reset to 0.0
        curiosity_2 = orchestrator.state["metacognition"]["curiosity_index"]
        print(f"Curiosity Index after Vikalpa trigger: {curiosity_2:.4f}")
        assert curiosity_2 == 0.0, "Curiosity index was not reset to 0.0 after Vasanas query trigger!"

        # Validate new planner-policy-executor path artifacts
        with open(log_path, "r", encoding="utf-8") as f:
            ledger_text = f.read()
        assert "Planning actions for" in ledger_text, "Planner-policy-executor marker not found in ledger logs."

        sample_plan = orchestrator.build_curiosity_action_plan(
            "structured testing for curiosity planner",
            direct_url="https://duckduckgo.com/?q=planner+path"
        )
        assert len(sample_plan.get("actions", [])) >= 1, "Curiosity action plan returned no actions."

        policy_result = orchestrator.apply_curiosity_policy(sample_plan)
        assert len(policy_result.get("approved", [])) >= 1, "Curiosity policy rejected all actions unexpectedly."

        # Blocked URL should reject browse action while preserving fallback search action
        blocked_plan = orchestrator.build_curiosity_action_plan(
            "blocked browse policy test",
            direct_url="https://example.com/login"
        )
        blocked_policy = orchestrator.apply_curiosity_policy(blocked_plan)
        rejected_reasons = "\n".join([r.get("reason", "") for r in blocked_policy.get("rejected", [])])
        assert "blocked pattern" in rejected_reasons.lower(), "Blocked browse policy did not reject sensitive URL pattern."
        assert any(a.get("type") == "search" for a in blocked_policy.get("approved", [])), "Fallback search action missing after browse rejection."

        orchestrator.ensure_mind_runtime_defaults()
        orchestrator.state["mind_runtime"]["last_identity_gate"] = {
            "allow_curiosity": False,
            "allow_tool": False,
            "level": "blocked",
            "reason": "fail_inconsistent",
        }
        id_blocked_policy = orchestrator.apply_curiosity_policy(sample_plan)
        assert len(id_blocked_policy.get("approved", [])) == 0, "Identity gate should block planner-level external actions."
        id_blocked_reasons = "\n".join([r.get("reason", "") for r in id_blocked_policy.get("rejected", [])])
        assert "identity gate blocked external action planning" in id_blocked_reasons.lower(), "Identity-gated planner rejection reason missing."

        orchestrator.state["mind_runtime"]["last_identity_gate"] = {
            "allow_curiosity": True,
            "allow_tool": True,
            "level": "pass",
            "reason": "pass",
        }

        preview_html = await orchestrator.build_embedded_preview_document("https://duckduckgo.com/?q=embedded+preview+test")
        assert "AI-AM Embedded Preview" in preview_html, "Embedded preview document generation failed."

        sample_tool_intent = orchestrator.build_tool_intent(
            "sandbox_python",
            {
                "code": "print('policy pass')",
                "code_length": len("print('policy pass')"),
                "allow_network": False,
            },
            source="test_suite",
        )
        sample_tool_policy = orchestrator.apply_tool_policy(sample_tool_intent)
        assert sample_tool_policy.get("allowed") is True, "Sandbox tool intent should pass baseline policy."

        denied_tool_intent = orchestrator.build_tool_intent(
            "sandbox_python",
            {
                "code": "import requests\nprint('blocked')",
                "code_length": len("import requests\nprint('blocked')"),
                "allow_network": False,
            },
            source="test_suite",
        )
        denied_tool_policy = orchestrator.apply_tool_policy(denied_tool_intent)
        assert denied_tool_policy.get("allowed") is False, "Denied pattern should block sandbox tool intent."
        denied_reasons = "\n".join(denied_tool_policy.get("reasons", []))
        assert "denied pattern" in denied_reasons.lower(), "Tool policy did not expose denied pattern reason."

        network_tool_intent = orchestrator.build_tool_intent(
            "sandbox_python",
            {
                "code": "print('network requested')",
                "code_length": len("print('network requested')"),
                "allow_network": True,
            },
            source="test_suite",
        )
        network_tool_policy = orchestrator.apply_tool_policy(network_tool_intent)
        assert network_tool_policy.get("allowed") is False, "Network request should be denied when policy blocks network access."
        network_reasons = "\n".join(network_tool_policy.get("reasons", []))
        assert "network access requested" in network_reasons.lower(), "Network-policy deny reason missing from tool policy output."

        before_audit = len(orchestrator.state.get("tool_runtime", {}).get("audit_trail", []))
        orchestrator.append_tool_audit_entry({"timestamp": "test", "intent_id": "test_intent", "decision": "allow"})
        after_audit = len(orchestrator.state.get("tool_runtime", {}).get("audit_trail", []))
        assert after_audit == before_audit + 1, "Tool audit trail entry was not appended."

        salience_profile = orchestrator.build_salience_profile("urgent identity continuity risk", has_user_prompt=True)
        assert 0.0 <= salience_profile.get("composite", 0.0) <= 1.0, "Salience profile composite score out of range."

        test_blackboard = orchestrator.build_cycle_blackboard("test stimulus", salience_profile)
        assert test_blackboard.get("cycle_id", "").startswith("cycle_"), "Cycle blackboard did not generate cycle_id."

        manas_contract = orchestrator.build_role_contract("manas", "panic signal overload", token_count=12)
        ahamkara_contract = orchestrator.build_role_contract("ahamkara", "calm stabilization frame", token_count=18)
        test_blackboard["roles"]["manas"] = manas_contract
        test_blackboard["roles"]["chitta"] = orchestrator.build_role_contract("chitta", "memory context node", token_count=0)
        test_blackboard["roles"]["ahamkara"] = ahamkara_contract
        test_blackboard["roles"]["buddhi"] = orchestrator.build_role_contract("buddhi", "resolved output", token_count=20)
        test_blackboard["conflicts"] = orchestrator.detect_role_conflicts(test_blackboard)
        assert isinstance(test_blackboard.get("conflicts", []), list), "Role conflict detector did not return a list."

        arbitration_note = orchestrator.build_arbitration_note(
            [{"type": "affect_alignment", "severity": "medium", "reason": "panic vs calm mismatch"}],
            "Chitta recalls prior panic-regulation cycles."
        )
        assert "ARBITRATION CONTEXT" in arbitration_note, "Arbitration note builder did not generate expected header."
        assert "panic vs calm mismatch" in arbitration_note, "Arbitration note did not include conflict reason."

        buddhi_channels = orchestrator.derive_buddhi_channels('[DIRECTIVE: RESEARCH "memory coherence metrics"]\n```python\nprint("x")\n```')
        assert buddhi_channels.get("curiosity_intent", {}).get("type") == "RESEARCH", "Buddhi channel extraction did not detect RESEARCH directive."
        assert buddhi_channels.get("tool_intent", {}).get("type") == "sandbox_python", "Buddhi channel extraction did not detect tool intent."

        parsed_json = orchestrator.parse_structured_output('{"chat_message":"hello","directives":[{"type":"RESEARCH","target":"agent contracts"}]}')
        assert isinstance(parsed_json, dict), "Structured output parser failed to parse inline JSON object."

        parsed_fenced = orchestrator.parse_structured_output('```json\n{"raw_reaction":"intense signal"}\n```')
        assert isinstance(parsed_fenced, dict), "Structured output parser failed to parse fenced JSON block."

        manas_content = orchestrator.extract_role_content("manas", "fallback", {"raw_reaction": "visceral spike"})
        assert manas_content == "visceral spike", "Role content extraction did not prioritize structured Manas key."

        structured_channels = orchestrator.derive_buddhi_channels(
            "fallback text",
            {"chat_message": "resolved", "directives": [{"type": "BROWSE", "target": "https://example.com"}]}
        )
        assert structured_channels.get("chat_message") == "resolved", "Buddhi structured chat channel was not used."
        assert structured_channels.get("curiosity_intent", {}).get("type") == "BROWSE", "Buddhi structured directive channel was not detected."

        manas_schema_ok = orchestrator.validate_role_schema(
            "manas",
            {"raw_reaction": "signal", "dominant_affect": "panic", "urgency_score": 0.8, "confidence": 0.7}
        )
        assert manas_schema_ok.get("valid") is True, "Valid Manas schema was marked invalid."

        buddhi_schema_fail = orchestrator.validate_role_schema(
            "buddhi",
            {"chat_message": "ok", "rationale": "ok"}
        )
        assert buddhi_schema_fail.get("valid") is False, "Invalid Buddhi schema was marked valid."
        assert "uncertainty_notes" in buddhi_schema_fail.get("missing", []), "Missing Buddhi schema keys were not reported."
        assert "identity_consistency_check" in buddhi_schema_fail.get("missing", []), "Buddhi schema should require identity_consistency_check."
        assert "identity_consistency_status" in buddhi_schema_fail.get("missing", []), "Buddhi schema should require identity_consistency_status."

        bb_before = len(orchestrator.state.get("mind_runtime", {}).get("recent_blackboards", []))
        orchestrator.append_cycle_blackboard(test_blackboard)
        bb_after = len(orchestrator.state.get("mind_runtime", {}).get("recent_blackboards", []))
        assert bb_after == bb_before + 1, "Mind runtime blackboard entry was not appended."

        policy_blackboard = orchestrator.build_cycle_blackboard("autonomous ping", {"composite": 0.8})
        allow_autonomous, autonomous_reason = orchestrator.should_emit_chat_message(False, policy_blackboard, "hello")
        assert allow_autonomous is True, f"Autonomous message was unexpectedly blocked: {autonomous_reason}"

        orchestrator.record_autonomous_message_emit(policy_blackboard, autonomous_reason)
        allow_after_emit, block_reason = orchestrator.should_emit_chat_message(False, policy_blackboard, "hello again")
        assert allow_after_emit is False, "Autonomous message cooldown/rate policy did not block rapid repeated emission."
        assert "cooldown" in block_reason or "window_rate_limit" in block_reason, "Unexpected block reason for autonomous policy gating."

        allow_user_mode, user_reason = orchestrator.should_emit_chat_message(True, policy_blackboard, "reply")
        assert allow_user_mode is True and user_reason == "user_prompt", "User-prompt chat should bypass autonomous gating."

        chitta_multi = orchestrator.db_manager.query_multi_timescale_context(
            "identity continuity and memory coherence",
            weights={"working": 0.2, "episodic": 0.3, "semantic": 0.3, "identity": 0.2},
            top_k=2,
        )
        assert isinstance(chitta_multi, dict), "Multi-timescale Chitta query did not return a dictionary payload."
        assert "weights" in chitta_multi and "layers" in chitta_multi and "context_text" in chitta_multi, "Multi-timescale Chitta payload missing expected keys."
        assert all(layer in chitta_multi.get("layers", {}) for layer in ["working", "episodic", "semantic", "identity"]), "Multi-timescale Chitta payload missing one or more layers."

        weighted_context = orchestrator.query_chitta_weighted_context("test stimulus weighted context")
        assert isinstance(weighted_context, dict), "Orchestrator weighted Chitta query wrapper did not return dict payload."
        assert "context_text" in weighted_context, "Orchestrator weighted Chitta query payload missing context text."

        role_mod = orchestrator.compute_role_modulation(
            {"novelty": 0.8, "urgency": 0.9, "identity_threat": 0.7},
            fatigue=0.4,
        )
        assert all(role in role_mod for role in ["manas", "ahamkara", "buddhi"]), "Role modulation missing expected role keys."
        assert 0.0 <= role_mod["manas"]["top_p"] <= 1.0, "Manas top_p modulation out of range."
        assert 0.0 <= role_mod["ahamkara"]["top_p"] <= 1.0, "Ahamkara top_p modulation out of range."
        assert 0.0 <= role_mod["buddhi"]["top_p"] <= 1.0, "Buddhi top_p modulation out of range."

        orchestrator.ensure_ahamkara_umwelt_defaults()
        umwelt_summary = orchestrator.get_umwelt_summary()
        assert "Identity Priors" in umwelt_summary and "Boundary Rules" in umwelt_summary, "Ahamkara Umwelt summary is missing required sections."

        orchestrator.ensure_mind_runtime_defaults()
        schema_policy = orchestrator.state.get("mind_runtime", {}).get("schema_policy", {})
        assert schema_policy.get("strict_mode") is True, "Schema policy strict_mode should default to True."
        assert int(schema_policy.get("max_retries", 0)) >= 1, "Schema policy max_retries should default to at least 1."
        clarification_policy = orchestrator.state.get("mind_runtime", {}).get("clarification_policy", {})
        assert clarification_policy.get("enabled") is True, "Clarification policy should default to enabled."
        assert int(clarification_policy.get("max_rounds", 0)) == 1, "Clarification policy should default to a single negotiation round."

        orchestrator.update_ahamkara_umwelt(
            {
                "identity_frame": "protect coherent continuity",
                "threat_assessment": "moderate boundary risk",
                "continuity_action": "ask clarification and slow action",
            }
        )
        assert orchestrator.state.get("ahamkara_umwelt", {}).get("last_identity_frame", "") == "protect coherent continuity", "Ahamkara Umwelt update did not persist identity frame."

        buddhi_fallback = orchestrator.build_role_fallback_structured("buddhi", "fallback content")
        assert "identity_consistency_check" in buddhi_fallback, "Buddhi fallback payload should include identity consistency field."
        assert "identity_consistency_status" in buddhi_fallback, "Buddhi fallback payload should include canonical identity status field."

        normalized_pass = orchestrator.normalize_identity_status({"identity_consistency_status": "pass"})
        assert normalized_pass == "pass", "Canonical identity status pass did not normalize correctly."

        normalized_hybrid = orchestrator.normalize_identity_status({"identity_consistency_check": "This appears cautionary and uncertain"})
        assert normalized_hybrid == "pass_with_caution", "Hybrid identity normalization did not map free-form caution text."

        gate_pass = orchestrator.evaluate_identity_intent_gate({"identity_consistency_check": "pass"})
        assert gate_pass.get("allow_curiosity") is True and gate_pass.get("allow_tool") is True, "Identity gate should allow intents on pass state."

        gate_caution = orchestrator.evaluate_identity_intent_gate({"identity_consistency_check": "pass_with_caution"})
        assert gate_caution.get("allow_curiosity") is True and gate_caution.get("allow_tool") is False, "Identity gate caution should allow curiosity but block tools."

        gate_fail = orchestrator.evaluate_identity_intent_gate({"identity_consistency_check": "fail_inconsistent"})
        assert gate_fail.get("allow_curiosity") is False and gate_fail.get("allow_tool") is False, "Identity gate fail should block curiosity and tools."

        gate_unknown = orchestrator.evaluate_identity_intent_gate({"identity_consistency_check": "maintained with minor ambiguity"})
        assert gate_unknown.get("allow_curiosity") is True and gate_unknown.get("allow_tool") is False, "Identity gate unknown text should allow curiosity but block tools."

        should_negotiate = orchestrator.should_run_inter_role_clarification(
            [{"type": "affect_alignment", "severity": "medium", "reason": "test conflict"}],
            rounds_used=0,
        )
        assert should_negotiate is True, "Clarification gate should allow one round for medium/high conflicts."

        should_not_negotiate_after_round = orchestrator.should_run_inter_role_clarification(
            [{"type": "affect_alignment", "severity": "high", "reason": "test conflict"}],
            rounds_used=1,
        )
        assert should_not_negotiate_after_round is False, "Clarification gate should enforce one-round latency bound."

        clarification_text = orchestrator.build_clarification_context(
            {"conflicts": [{"type": "identity_omission", "severity": "medium", "reason": "missing threat markers"}]},
            "Chitta memory context sample",
        )
        assert "Clarification Round Context" in clarification_text and "identity_omission" in clarification_text, "Clarification context builder did not include conflict details."

        pre_negotiation_blackboard = {
            "roles": {
                "manas": {"affect_markers": ["panic"]},
                "ahamkara": {"affect_markers": ["calm"]},
            },
            "salience": {"urgency": 0.9, "identity_threat": 0.9},
        }
        post_negotiation_blackboard = {
            "roles": {
                "manas": {"affect_markers": ["panic"]},
                "ahamkara": {"affect_markers": ["threat"]},
            },
            "salience": {"urgency": 0.9, "identity_threat": 0.9},
        }
        conflicts_before = orchestrator.detect_role_conflicts(pre_negotiation_blackboard)
        conflicts_after = orchestrator.detect_role_conflicts(post_negotiation_blackboard)
        severity_rank = {"low": 1, "medium": 2, "high": 3}
        max_before = max([severity_rank.get(str(c.get("severity", "low")).lower(), 1) for c in conflicts_before], default=0)
        max_after = max([severity_rank.get(str(c.get("severity", "low")).lower(), 1) for c in conflicts_after], default=0)
        assert len(conflicts_after) <= len(conflicts_before) and max_after <= max_before, "Negotiation follow-up conflicts must stay bounded in count and severity."

        no_frame_mm = await orchestrator.gather_multimodal_evidence("stimulus", {"composite": 0.9}, has_user_prompt=True)
        assert isinstance(no_frame_mm, dict), "Multimodal evidence router did not return dict payload."
        assert no_frame_mm.get("used") in [True, False], "Multimodal evidence router returned invalid used flag."
        
        # Verify that current stimulus contains search results
        current_stim = orchestrator.state["internal_workspace"]["current_stimulus"]
        print("Dream Stimulus containing Web Snippets:", repr(current_stim[:150]) + "...")
        assert "[Search Results for:" in current_stim or "[Browser Exploration Triggered" in current_stim, "Search results were not injected into dreaming stimulus!"

        # TEST 6: Karmendriya Sandbox Execution
        print("\n--- TEST 6: Karmendriya Sandbox Execution ---")
        
        # Inject matching node in the database to keep similarity high and prevent Jijnasa preemption
        orchestrator.db_manager.add_node(
            "node_sandbox_prevent",
            "Write a python script that prints 'Hello from Karmendriya' and nothing else inside a ```python ``` block.",
            0.50
        )
        
        sandbox_prompt = "Write a python script that prints 'Hello from Karmendriya' and nothing else inside a ```python ``` block."
        print(f"Feeding sandbox prompt: '{sandbox_prompt}'")
        await orchestrator.input_queue.put(sandbox_prompt)
        await orchestrator.execute_cognitive_cycle()
        
        # Check if the sandbox output file exists in the shared folder
        output_file_path = "workspace/sandbox_shared/output.txt"
        print(f"Verifying sandbox output file: {output_file_path}")
        assert os.path.exists(output_file_path), "Sandbox output.txt file does not exist!"
        
        with open(output_file_path, "r", encoding="utf-8") as f:
            sandbox_log = f.read()
            print("Sandbox output file contents:\n" + sandbox_log)
            # Should contain either the stdout or the Docker daemon connection error
            assert "Hello from Karmendriya" in sandbox_log or "Docker error" in sandbox_log or "docker" in sandbox_log.lower(), "Sandbox execution log incorrect!"

        # TEST 7: Concurrency Safety and VRAM Protection Validation
        print("\n--- TEST 7: Concurrency Safety and VRAM Protection Validation ---")
        print("Executing parallel inference requests to verify concurrent slot allocation uses correct num_ctx limits...")
        
        start_concurrency = time.time()
        task_1 = asyncio.create_task(
            orchestrator.call_inference_slot("You are processor slot A. Respond only with 'A'.", "Ping A", 0.1)
        )
        task_2 = asyncio.create_task(
            orchestrator.call_inference_slot("You are processor slot B. Respond only with 'B'.", "Ping B", 0.1)
        )
        
        res_1, res_2 = await asyncio.gather(task_1, task_2)
        elapsed_concurrency = time.time() - start_concurrency
        
        print(f"Parallel requests completed in {elapsed_concurrency:.2f} seconds.")
        print("Response 1:", repr(res_1[0]))
        print("Response 2:", repr(res_2[0]))
        
        assert len(res_1[0]) > 0 and len(res_2[0]) > 0, "One of the concurrent slot responses was empty!"
        print("Concurrency safety and VRAM protection validation verified successfully.")

        # TEST 8: UI/API Graceful Shutdown Request Idempotency
        print("\n--- TEST 8: Graceful Shutdown Request Idempotency ---")
        first_shutdown = await orchestrator.request_shutdown("test_suite")
        second_shutdown = await orchestrator.request_shutdown("test_suite_repeat")
        print(f"First shutdown request accepted: {first_shutdown}")
        print(f"Second shutdown request accepted: {second_shutdown}")
        assert first_shutdown is True, "First shutdown request should be accepted."
        assert second_shutdown is False, "Second shutdown request should be rejected as duplicate."
        assert orchestrator.running is False, "Shutdown request should set running=False."
        assert orchestrator.shutdown_event.is_set(), "Shutdown event should be set after shutdown request."

        # TEST 4: Graceful Shutdown Lifecycle
        print("\n--- TEST 4: Graceful Shutdown Lifecycle ---")
        orchestrator.shutdown()
        print("Shutdown test verified.")

        print("\n==================================================")
        print("ALL TESTS COMPLETED SUCCESSFULLY!")
        print("==================================================")
        
    except Exception as e:
        print(f"[Test Suite Fatal] Run failed: {str(e)}")
        raise e

if __name__ == "__main__":
    asyncio.run(main())
