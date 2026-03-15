# Pharmacy and Pharmacist Registration Guide

This guide explains how to register pharmacies and pharmacists in the system so they can access their dashboards.

## Overview

The system requires:
1. **Pharmacy Registration**: Register a pharmacy first
2. **Pharmacist Registration**: Register pharmacists and link them to a pharmacy

Each pharmacist gets their own dashboard to view and respond to medicine requests.

## Registration Endpoints

### 1. Register a Pharmacy

**Endpoint**: `POST /api/chatbot/register/pharmacy/`

**Request Body**:
```json
{
  "pharmacy_id": "ph-001",
  "name": "HealthFirst Pharmacy",
  "address": "123 Main Street, Harare",
  "latitude": -17.8095,
  "longitude": 31.0452,
  "phone": "+263771234567",
  "email": "info@healthfirst.co.zw"
}
```

**Required Fields**:
- `pharmacy_id`: Unique identifier (min 3 characters)
- `name`: Pharmacy name
- `address`: Full address

**Optional Fields**:
- `latitude`, `longitude`: GPS coordinates
- `phone`: Contact number
- `email`: Contact email

**Response** (201 Created):
```json
{
  "message": "Pharmacy registered successfully",
  "pharmacy": {
    "pharmacy_id": "ph-001",
    "name": "HealthFirst Pharmacy",
    "address": "123 Main Street, Harare",
    "latitude": -17.8095,
    "longitude": 31.0452,
    "phone": "+263771234567",
    "email": "info@healthfirst.co.zw",
    "is_active": true,
    "created_at": "2026-01-12T15:30:00Z"
  }
}
```

### 2. Register a Pharmacist

**Endpoint**: `POST /api/chatbot/register/pharmacist/`

**Request Body**:
```json
{
  "pharmacy_id": "ph-001",
  "first_name": "John",
  "last_name": "Doe",
  "email": "john.doe@healthfirst.co.zw",
  "phone": "+263771234568",
  "license_number": "PHARM-2024-001",
  "username": "johndoe",
  "password": "securepassword123"
}
```

**Required Fields**:
- `pharmacy_id`: ID of the pharmacy this pharmacist belongs to
- `first_name`, `last_name`: Pharmacist's name
- `email`: Unique email address
- `username`: Unique username for Django authentication
- `password`: Password (min 8 characters)

**Optional Fields**:
- `phone`: Contact number
- `license_number`: Pharmacist license/registration number

**Response** (201 Created):
```json
{
  "message": "Pharmacist registered successfully",
  "pharmacist": {
    "pharmacist_id": "550e8400-e29b-41d4-a716-446655440000",
    "pharmacy": {
      "pharmacy_id": "ph-001",
      "name": "HealthFirst Pharmacy",
      ...
    },
    "first_name": "John",
    "last_name": "Doe",
    "full_name": "John Doe",
    "email": "john.doe@healthfirst.co.zw",
    "phone": "+263771234568",
    "license_number": "PHARM-2024-001",
    "is_active": true,
    "created_at": "2026-01-12T15:35:00Z"
  }
}
```

## Listing Endpoints

### List All Pharmacies

**Endpoint**: `GET /api/chatbot/pharmacies/`

**Response**:
```json
[
  {
    "pharmacy_id": "ph-001",
    "name": "HealthFirst Pharmacy",
    "address": "123 Main Street, Harare",
    ...
  },
  ...
]
```

### List All Pharmacists

**Endpoint**: `GET /api/chatbot/pharmacists/`

**Response**: List of all active pharmacists

### List Pharmacists by Pharmacy

**Endpoint**: `GET /api/chatbot/pharmacists/{pharmacy_id}/`

**Example**: `GET /api/chatbot/pharmacists/ph-001/`

**Response**: List of pharmacists for that specific pharmacy

## Pharmacist Login

After registration, pharmacists can log in using their email and password.

**Endpoint**: `POST /api/chatbot/pharmacist/login/`

**Request Body**:
```json
{
  "email": "john.doe@healthfirst.co.zw",
  "password": "securepassword123"
}
```

**Response** (200 OK):
```json
{
  "pharmacist": {
    "pharmacist_id": "550e8400-e29b-41d4-a716-446655440000",
    "pharmacy": {...},
    "first_name": "John",
    "last_name": "Doe",
    ...
  },
  "message": "Login successful"
}
```

## Workflow Example

### Step 1: Register Pharmacy
```bash
curl -X POST http://localhost:8000/api/chatbot/register/pharmacy/ \
  -H "Content-Type: application/json" \
  -d '{
    "pharmacy_id": "ph-001",
    "name": "HealthFirst Pharmacy",
    "address": "123 Main Street, Harare",
    "latitude": -17.8095,
    "longitude": 31.0452,
    "phone": "+263771234567",
    "email": "info@healthfirst.co.zw"
  }'
```

### Step 2: Register First Pharmacist
```bash
curl -X POST http://localhost:8000/api/chatbot/register/pharmacist/ \
  -H "Content-Type: application/json" \
  -d '{
    "pharmacy_id": "ph-001",
    "first_name": "John",
    "last_name": "Doe",
    "email": "john.doe@healthfirst.co.zw",
    "username": "johndoe",
    "password": "securepassword123",
    "license_number": "PHARM-2024-001"
  }'
```

### Step 3: Register Additional Pharmacists
Repeat Step 2 for each pharmacist (typically 2-3 per pharmacy).

### Step 4: Login and Access Dashboard
```bash
curl -X POST http://localhost:8000/api/chatbot/pharmacist/login/ \
  -H "Content-Type: application/json" \
  -d '{
    "email": "john.doe@healthfirst.co.zw",
    "password": "securepassword123"
  }'
```

Use the returned `pharmacist_id` to access the dashboard:
```bash
curl -X GET "http://localhost:8000/api/chatbot/pharmacist/requests/?pharmacist_id={pharmacist_id}"
```

## Django Admin Interface

You can also register pharmacies and pharmacists through the Django admin panel:

1. Access: `http://localhost:8000/admin/`
2. Login with superuser credentials
3. Navigate to **Chatbot** section
4. Add **Pharmacy** entries
5. Add **Pharmacist** entries (link to pharmacy)

## Important Notes

1. **Pharmacy ID must be unique**: Choose a meaningful ID (e.g., "ph-001", "city-care-harare")
2. **Email must be unique**: Each pharmacist must have a unique email
3. **Username must be unique**: Django username must be unique across all users
4. **Password security**: Use strong passwords (min 8 characters)
5. **Multiple pharmacists**: Each pharmacy can have multiple pharmacists (typically 2-3)
6. **Dashboard access**: Each pharmacist has their own dashboard showing requests for their pharmacy

## Error Handling

### Pharmacy Already Exists
```json
{
  "error": "Pharmacy with ID 'ph-001' already exists"
}
```

### Pharmacist Email Already Exists
```json
{
  "error": "A pharmacist with this email already exists"
}
```

### Pharmacy Not Found
```json
{
  "error": "Pharmacy with ID 'ph-001' not found or inactive"
}
```

### Invalid Credentials
```json
{
  "error": "Invalid credentials"
}
```
