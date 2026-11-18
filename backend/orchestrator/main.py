from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routes import router as rest_router
from websocket import router as websocket_router


app = FastAPI(
    title="Day2 BP Chat API",
    description="Chat API with hybrid retrieval",
    version="1.0.0",
)

# Allow the Vite dev server to call the REST API from the browser
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(rest_router, prefix="/api")
app.include_router(websocket_router)


@app.get("/")
def root():
    return {
        "message": "Day2 BP Chat API is running"
    }