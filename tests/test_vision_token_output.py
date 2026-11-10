from services.vision.token_output import completion_tokens


def test_generation_handles_prompt_prefix_and_completion_only():
    assert completion_tokens([1, 2, 3], [1, 2, 3, 4, 5]) == [4, 5]
    assert completion_tokens([1, 2, 3], [4, 5]) == [4, 5]
    assert completion_tokens([1, 2], [4, 5, 6]) == [4, 5, 6]
    assert completion_tokens([1], []) == []
