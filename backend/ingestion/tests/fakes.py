"""Small stand-ins shared by several test files."""


class FakeGroq:
    """Mimics client.chat.completions.create(...). `replies` are returned (or
    raised, if they are exceptions) one per call, in order."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply

        class Message:
            content = reply

        class Choice:
            message = Message()

        class Response:
            choices = [Choice()]

        return Response()


class HttpError(Exception):
    def __init__(self, status_code, message=""):
        super().__init__(message)
        self.status_code = status_code


class APITimeoutError(Exception):
    pass


class FakeQdrant:
    """Just enough of QdrantClient: stores points, applies must-filters."""

    def __init__(self):
        self.points = {}  # id -> {"vector":..., "payload": {...}}
        self.exists = True
        self.calls = []

    def collection_exists(self, name):
        return self.exists

    def _matches(self, point, flt):
        for condition in flt.must:
            if point["payload"].get(condition.key) != condition.match.value:
                return False
        return True

    def upsert(self, collection_name, points, wait=False):
        self.calls.append("upsert")
        for point in points:
            self.points[point.id] = {"vector": point.vector, "payload": dict(point.payload)}

    def set_payload(self, collection_name, payload, points, wait=False):
        self.calls.append("set_payload")
        for point in self.points.values():
            if self._matches(point, points):
                point["payload"].update(payload)

    def delete(self, collection_name, points_selector, wait=False):
        self.calls.append("delete")
        flt = points_selector.filter
        for point_id in [i for i, p in self.points.items() if self._matches(p, flt)]:
            del self.points[point_id]
