# Architecture

The project follows ports and adapters: web/API calls application services; application services
coordinate domain rules through ports; game, vision, storage, clock, and artifact implementations are
adapters. The domain layer has no FastAPI, SQLAlchemy, OpenCV, YOLO, or PyAutoGUI dependency.
