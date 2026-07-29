def test_real_fixed_parameter_minimal_inference_under_p1b2_authorization(
    authorized_gpu_model_root,
):
    from materialsagent_zta35g_runtime.inference import (
        TorchZTA35GComponents,
        ZTA35GInferenceEngine,
    )

    engine = ZTA35GInferenceEngine(
        TorchZTA35GComponents(authorized_gpu_model_root)
    )
    engine.load()
    try:
        assert engine.device_kind == "cuda"
        result = engine.execute(
            {
                "solution_temperature": 1000,
                "solution_time": 3.0,
                "aging_temperature": 730,
                "aging_time": 3.0,
            },
            ("sem_image", "mechanical_properties"),
            {
                "seed": 12345,
                "num_samples": 1,
                "guide_scale": 2.0,
                "timesteps": 1000,
            },
        )
        assert result.status == "SUCCEEDED"
    finally:
        engine.close()
