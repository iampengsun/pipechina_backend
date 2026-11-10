"""Handle both prompt-prefixed and completion-only model generation results."""


def completion_tokens(prompt, sequence):
    prompt_ids = prompt.tolist() if hasattr(prompt, "tolist") else list(prompt)
    prefix = sequence[: len(prompt_ids)]
    prefix_ids = prefix.tolist() if hasattr(prefix, "tolist") else list(prefix)
    if prefix_ids == prompt_ids:
        return sequence[len(prompt_ids) :]
    return sequence
