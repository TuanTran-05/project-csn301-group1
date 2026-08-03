class SmokeChatProvider:
    def complete(self, message: str) -> str:
        return '{"intent":"chat","message":"Scoped network assistance is available.","operations":[]}'

class RecordingProvider:
    def __init__(self, provider): self.provider=provider; self.records=[]
    def complete(self, message):
        import time
        started=time.perf_counter(); raw=self.provider.complete(message); latency=(time.perf_counter()-started)*1000
        self.records.append({"message":message,"raw":raw,"latency_ms":latency}); return raw
