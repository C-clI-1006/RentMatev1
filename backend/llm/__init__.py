"""LLM adapter (shared capability).

Single point for all model calls: config (base URL / model / key from
env), timeouts, structured-output validation, error conversion to
recognised failure results. Business modules never call the model
directly (guide Ch.19).

Until the school Qwen proxy is confirmed working, modules must use
labelled test doubles (stub classifiers/drafters) that keep the final
interface shapes. Do not present mocked inference as evidence of a
live agent.
"""
