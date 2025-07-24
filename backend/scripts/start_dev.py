import uvicorn

def main():
    uvicorn.run("gamegen-backend.asgi:application", host="localhost", port=8000, reload=True, timeout_keep_alive=600)

if __name__ == "__main__":
    main()