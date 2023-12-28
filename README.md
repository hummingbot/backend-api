# Backend API 

## Overview
Backend-api is a dedicated solution for managing Hummingbot instances. It offers a robust backend API to streamline the deployment, management, and interaction with Hummingbot containers. This tool is essential for administrators and developers looking to efficiently handle various aspects of Hummingbot operations.

## Features
- **Deployment File Management**: Manage files necessary for deploying new Hummingbot instances.
- **Container Control**: Effortlessly start and stop Hummingbot containers.
- **Archiving Options**: Securely archive containers either locally or on Amazon S3 post-removal.
- **Direct Messaging**: Communicate with Hummingbots through the broker for effective control and coordination.

## Getting Started

### Conda Installation
1. Install the environment using Conda:
   ```bash
   conda env create -f environment.yml
   ```
2. Activate the Conda environment:
   ```bash
   conda activate backend-api
   ```

### Running the API with Conda
Run the API using uvicorn with the following command:
   ```bash
   uvicorn main:app --reload
   ```

### Docker Installation and Running the API
For running the project using Docker, follow these steps:

1. **Set up Environment Variables**:
   - Execute the `set_environment.sh` script to configure the necessary environment variables in the `.env` file:
     ```bash
     ./set_environment.sh
     ```

2. **Build and Run with Docker Compose**:
   - After setting up the environment variables, use Docker Compose to build and run the project:
     ```bash
     docker compose up --build
     ```

   - This command will build the Docker image and start the containers as defined in your `docker-compose.yml` file.

### Usage
This API is designed for:
- **Deploying Hummingbot instances**
- **Starting/Stopping Containers**
- **Archiving Hummingbots**
- **Messaging with Hummingbot instances**

To test these endpoints, you can use the [Swagger UI](http://localhost:8000/docs) or [Redoc](http://localhost:8000/redoc).

## Contributing
Contributions are welcome! For support or queries, please contact us on Discord.
