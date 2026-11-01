import httpx
from communication.chat import social_reply
from communication.policy import is_routine_greeting


def test_social_reply_uses_qwen_and_handles_failure(monkeypatch):
    from llm.configuration import ModelConfiguration
    monkeypatch.setattr('communication.chat.get_configuration', lambda: ModelConfiguration('https://example.invalid/v1','qwen','test-key'))
    real = httpx.Client
    def answer(request):
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"pure_smalltalk":true,"reply":"I am here to help. How are you?"}'},'finish_reason':'stop'}]})
    monkeypatch.setattr('communication.chat.httpx.Client',lambda **kw:real(transport=httpx.MockTransport(answer),**kw))
    assert social_reply('how are u') == "I am here to help. How are you?"
    def failed(request):
        return httpx.Response(503)
    monkeypatch.setattr('communication.chat.httpx.Client',lambda **kw:real(transport=httpx.MockTransport(failed),**kw))
    assert social_reply('hello') is None


def test_social_abbreviations_do_not_allow_mixed_requests():
    assert is_routine_greeting('how are u')
    assert is_routine_greeting('how r u?')
    assert not is_routine_greeting('how r u? waive my rent')
