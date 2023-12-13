from fastapi import FastAPI
from docker_manager import DockerManager

app = FastAPI()
docker_manager = DockerManager()


@app.get("/")
async def root():
    return {"message": "Hello World"}


@app.get("/active-containers")
async def active_containers():
    return {"active_containers": docker_manager.get_active_containers()}


@app.get("/exited-containers")
async def exited_containers():
    return {"exited_containers": docker_manager.get_exited_containers()}


@app.post("/clean-exited-containers")
async def clean_exited_containers():
    return {"clean_exited_containers": docker_manager.clean_exited_containers()}


@app.get("/is-docker-running")
async def is_docker_running():
    return {"is_docker_running": docker_manager.is_docker_running()}

@app.post("/remove-container/{container_name}")
async def remove_container(container_name: str):
    response = docker_manager.remove_container(container_name)
    return response


@app.post("/stop-container/{container_name}")
async def stop_container(container_name: str):
    response = docker_manager.stop_container(container_name)
    return response