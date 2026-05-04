from textwrap import dedent

import yaml

from baseten.client.modelconfig import ModelConfig


def test_vllm_config():
    # From truss-examples/vllm/config.yaml
    config = ModelConfig.model_validate(
        yaml.safe_load(
            dedent("""
        model_name: "Llama 3.1 8B Instruct VLLM openai compatible"
        python_version: py311
        model_metadata:
          example_model_input: {"prompt": "what is the meaning of life"}
          repo_id: meta-llama/Llama-3.1-8B-Instruct
          openai_compatible: true
        requirements:
          - vllm==0.5.4
        resources:
          accelerator: A100
          use_gpu: true
        runtime:
          predict_concurrency: 128
        secrets:
          hf_access_token: null
    """)
        )
    )
    assert config.model_name == "Llama 3.1 8B Instruct VLLM openai compatible"
    assert config.python_version == "py311"
    assert config.requirements == ["vllm==0.5.4"]
    assert config.resources is not None
    assert config.resources.accelerator is not None
    assert config.resources.accelerator.root == "A100"
    assert config.resources.model_extra == {"use_gpu": True}
    assert config.runtime is not None
    assert config.runtime.predict_concurrency == 128


def test_whisper_config():
    # From truss-examples/07-high-performance-dynamic-batching/config.yaml
    config = ModelConfig.model_validate(
        yaml.safe_load(
            dedent("""
        base_image:
          image: baseten/trtllm-server:r23.12_baseten_v0.9.0.dev2024022000
          python_executable_path: /usr/bin/python3
        model_name: TRT Whisper - Dynamic Batching
        python_version: py311
        model_cache:
          - repo_id: baseten/trtllm-whisper-a10g-large-v2-1
            revision: main
            use_volume: true
            volume_folder: trtllm-whisper-a10g-large-v2-1
        resources:
          accelerator: A10G
        runtime:
          predict_concurrency: 256
        external_data:
          - local_data_path: assets/multilingual.tiktoken
            url: https://raw.githubusercontent.com/openai/whisper/main/whisper/assets/multilingual.tiktoken
    """)
        )
    )
    assert config.model_name == "TRT Whisper - Dynamic Batching"
    assert config.base_image is not None
    assert (
        config.base_image.image
        == "baseten/trtllm-server:r23.12_baseten_v0.9.0.dev2024022000"
    )
    assert config.model_cache is not None
    assert len(config.model_cache.root) == 1
    assert (
        config.model_cache.root[0].repo_id == "baseten/trtllm-whisper-a10g-large-v2-1"
    )
    assert config.model_cache.root[0].use_volume is True
    assert config.external_data is not None
    assert len(config.external_data.root) == 1
    assert (
        config.external_data.root[0].local_data_path == "assets/multilingual.tiktoken"
    )


def test_chatterbox_config():
    # From truss-examples/chatterbox-tts/config.yaml
    config = ModelConfig.model_validate(
        yaml.safe_load(
            dedent("""
        model_name: Chatterbox TTS
        base_image:
          image: jojobaseten/truss-numpy-1.26.0-gpu:0.4
          python_executable_path: /usr/bin/python3
        python_version: py312
        requirements:
          - chatterbox-tts
        resources:
          accelerator: H100
          cpu: '1'
          memory: 40Gi
          use_gpu: true
    """)
        )
    )
    assert config.model_name == "Chatterbox TTS"
    assert config.python_version == "py312"
    assert config.resources is not None
    assert config.resources.accelerator is not None
    assert config.resources.accelerator.root == "H100"
    assert config.resources.cpu == "1"
    assert config.resources.memory == "40Gi"
    assert config.resources.model_extra == {"use_gpu": True}
