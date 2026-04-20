# PDF to Online Test System

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Backend-green.svg)](https://fastapi.tiangolo.com/)
[![PyMuPDF](https://img.shields.io/badge/PyMuPDF-PDF%20Processing-orange.svg)](https://pymupdf.readthedocs.io/)
[![Pillow](https://img.shields.io/badge/Pillow-Image%20Processing-yellow.svg)](https://python-pillow.org/)
[![NestJS](https://img.shields.io/badge/NestJS-Backend-red.svg)](https://nestjs.com/)
[![Socket.IO](https://img.shields.io/badge/Socket.IO-Realtime-black.svg)](https://socket.io/)
[![MySQL](https://img.shields.io/badge/MySQL-Database-blue.svg)](https://www.mysql.com/)
[![TypeORM](https://img.shields.io/badge/TypeORM-ORM-lightgrey.svg)](https://typeorm.io/)
[![OAuth2](https://img.shields.io/badge/OAuth2-Auth-purple.svg)](https://oauth.net/2/)
[![JWT](https://img.shields.io/badge/JWT-Auth-critical.svg)](https://jwt.io/)

## Overview

This project converts PDF exam files into structured online tests.

It extracts questions from PDF documents, processes layout information, and transforms them into a format suitable for web-based testing systems.

---

## Tech Stack

### Core Processing

- Python
- PyMuPDF (PDF parsing & layout extraction)
- Pillow (image processing)

### Backend Services

#### Test Generation Service

- FastAPI (Python)
- Responsible for:
  - Parsing PDF files
  - Extracting questions
  - Generating test content (images + metadata)

#### System Services

- NestJS (Node.js)
- Handles:
  - Authentication & Identity
  - Classroom management
  - Main application APIs
  - Real-time notifications (Socket.IO)

### Database & ORM

- MySQL
- TypeORM

### Authentication

- OAuth2 for user login
- JWT for securing communication between services

---

## Architecture

The system uses a simple multi-service backend approach:

- A Python service (FastAPI) dedicated to heavy processing (PDF parsing and test generation)
- A Node.js service (NestJS) handling the main application logic

### Communication

The flow is roughly:

1. User authenticates via OAuth2
2. NestJS issues a JWT
3. Client sends a request to generate a test
4. NestJS forwards the request to FastAPI with the JWT
5. FastAPI processes the PDF and returns the result

This is not a fully formalized architecture yet, but the idea is:

- keep heavy processing isolated
- keep the main backend responsive

At the moment, services communicate via HTTP.  
In the future, this could be replaced or extended with a message queue for async processing.

---

## Features

- Extract questions from PDF files
- Detect question headers (e.g. "Câu", "Question", "Task")
- Slice PDF into question-level images
- Handle multi-page questions
- Remove spacing artifacts when merging spans

---

## Processing Strategy

Instead of treating PDFs as plain text, this project uses layout-based parsing:

- Detect key spans using keywords + style (bold, position)
- Sort spans by page and vertical position
- Split the document into segments based on span positions
- Render each segment as an image
- Merge segments across pages when needed

This allows reconstructing the structure of exam questions more reliably.

---

## Limitations

- Depends heavily on PDF formatting
- Bold detection is heuristic-based
- Keyword matching is not fully robust
- Multi-column layouts are not well supported

---

## Future Improvements

- Better layout detection (tables, multi-column PDFs)
- Detect answers (A, B, C, D)
- Export structured JSON instead of only images
- Introduce async processing (queue-based)
- Improve storage strategy (e.g. cloud storage)
- OCR fallback for scanned PDFs

---

## Notes

This project is part experiment, part real-world system.

Some parts of the architecture are still evolving, especially around:

- service boundaries
- async processing
- scaling

The current design works, but will likely change as the system grows.
