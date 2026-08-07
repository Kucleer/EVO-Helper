# Architecture

The project follows ports and adapters: web/API calls application services; application services
coordinate domain rules through ports; game, vision, storage, clock, and artifact implementations are
adapters. The domain layer has no FastAPI, SQLAlchemy, OpenCV, YOLO, or PyAutoGUI dependency.

`SqlAlchemyArtifactStore` writes immutable evidence into a configured local root and indexes its
relative path, media type, source, timestamp, and SHA-256 in `artifacts`. `SqlAlchemyUiObservationStore`
then records versioned UI observations only when their evidence artifact is already indexed.
