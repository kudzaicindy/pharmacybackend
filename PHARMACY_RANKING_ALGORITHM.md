# Pharmacy Response Ranking Algorithm

## Overview

When pharmacies submit responses to medicine requests, they are automatically ranked using a sophisticated algorithm that considers multiple factors to find the best options for patients.

## Ranking Factors

The algorithm uses a **scoring system** where **lower scores = better rank**. The algorithm considers:

### 1. **Availability** (Most Important)
- **Weight**: 1000 points penalty if unavailable
- **Logic**: Medicines that are not available get a massive penalty, pushing them to the bottom
- **Formula**: `score += 1000 if not available`

### 2. **Total Time** (Very Important)
- **Weight**: 2 points per minute
- **Logic**: Faster is better (preparation time + travel time)
- **Formula**: `score += total_time_minutes * 2`
- **Total Time** = `preparation_time + estimated_travel_time`

### 3. **Price** (Important)
- **Weight**: 10 points per currency unit
- **Logic**: Lower price is better (only applies if medicine is available)
- **Formula**: `score += price * 10` (if available)

### 4. **Distance** (Moderately Important)
- **Weight**: 5 points per kilometer
- **Logic**: Closer pharmacies are preferred
- **Formula**: `score += distance_km * 5`

## Ranking Formula

```python
score = 0

# Availability (most important)
if not medicine_available:
    score += 1000

# Time factor
total_time = preparation_time + estimated_travel_time
score += total_time * 2

# Price factor (only if available)
if medicine_available and price:
    score += price * 10

# Distance factor
if distance_km:
    score += distance_km * 5

# Lower score = better rank
```

## Example Rankings

### Example 1: All Available

| Pharmacy | Available | Price | Prep Time | Travel Time | Distance | Score | Rank |
|----------|-----------|-------|-----------|-------------|----------|-------|------|
| Pharmacy A | ✅ Yes | $3.80 | 15 min | 5 min | 0.8 km | 63 | 1 |
| Pharmacy B | ✅ Yes | $4.50 | 20 min | 7 min | 1.2 km | 89 | 2 |
| Pharmacy C | ✅ Yes | $5.20 | 30 min | 10 min | 2.0 km | 142 | 3 |

**Calculation for Pharmacy A:**
- Time: (15 + 5) * 2 = 40
- Price: 3.80 * 10 = 38
- Distance: 0.8 * 5 = 4
- **Total Score: 82** ✅ Best

### Example 2: Mixed Availability

| Pharmacy | Available | Price | Prep Time | Travel Time | Distance | Score | Rank |
|----------|-----------|-------|-----------|-------------|----------|-------|------|
| Pharmacy A | ✅ Yes | $4.50 | 15 min | 5 min | 1.0 km | 70 | 1 |
| Pharmacy B | ❌ No | - | 10 min | 3 min | 0.5 km | 1026 | 3 |
| Pharmacy C | ✅ Yes | $5.00 | 20 min | 8 min | 1.5 km | 98 | 2 |

**Calculation for Pharmacy B (Unavailable):**
- Availability penalty: 1000
- Time: (10 + 3) * 2 = 26
- Distance: 0.5 * 5 = 2.5
- **Total Score: 1028.5** ❌ Ranked last despite being closest

## Top 3 Responses

By default, the system returns the **top 3 ranked responses** to the user. This provides:
- ✅ Best options without overwhelming the user
- ✅ Variety of choices (different pharmacies, prices, times)
- ✅ Fast decision-making

## API Usage

### Get Ranked Responses

**Endpoint:** `GET /api/chatbot/request/{request_id}/ranked/`

**Query Parameters:**
- `limit` (optional): Number of top responses to return (default: 3)

**Example:**
```bash
# Get top 3 (default)
GET /api/chatbot/request/abc-123/ranked/

# Get top 5
GET /api/chatbot/request/abc-123/ranked/?limit=5
```

**Response:**
```json
[
  {
    "rank": 1,
    "pharmacy_name": "HealthFirst Pharmacy",
    "medicine_available": true,
    "price": "3.80",
    "preparation_time": 15,
    "estimated_travel_time": 5,
    "total_time_minutes": 20,
    "distance_km": 0.8,
    "ranking_score": 63,
    ...
  },
  {
    "rank": 2,
    "pharmacy_name": "City Care Pharmacy",
    "medicine_available": true,
    "price": "4.50",
    "preparation_time": 20,
    "estimated_travel_time": 7,
    "total_time_minutes": 27,
    "distance_km": 1.2,
    "ranking_score": 89,
    ...
  },
  {
    "rank": 3,
    "pharmacy_name": "Wellness Pharmacy",
    "medicine_available": true,
    "price": "5.20",
    "preparation_time": 30,
    "estimated_travel_time": 10,
    "total_time_minutes": 40,
    "distance_km": 2.0,
    "ranking_score": 142,
    ...
  }
]
```

## Automatic Ranking in Chat Response

When a user provides location and creates a medicine request, the chat response automatically includes the **top 3 ranked pharmacy responses**:

```json
{
  "response": "✅ Your request has been sent to nearby pharmacies! I found 3 top pharmacies with available options...",
  "medicine_request_id": "uuid",
  "pharmacy_responses": [
    { "rank": 1, ... },
    { "rank": 2, ... },
    { "rank": 3, ... }
  ],
  "request_sent_to_pharmacies": true,
  "total_responses": 3
}
```

## How Pharmacies Submit Responses

Pharmacies submit responses via the API endpoint:

**Endpoint:** `POST /api/chatbot/pharmacist/response/{request_id}/`

**Request Body:**
```json
{
  "pharmacist_id": "uuid",
  "medicine_available": true,
  "price": 4.50,
  "preparation_time": 15,
  "alternative_medicines": ["ibuprofen"],
  "notes": "Available in stock"
}
```

The system automatically:
1. Calculates distance from patient location
2. Estimates travel time
3. Ranks the response when user requests results

## Ranking Priority Order

1. **Availability** (must be available to rank high)
2. **Total Time** (preparation + travel)
3. **Price** (lower is better)
4. **Distance** (closer is better)

## Customization

To adjust ranking weights, modify the `get_ranked_pharmacy_responses()` function in `chatbot/views.py`:

```python
# Current weights
availability_penalty = 1000
time_weight = 2
price_weight = 10
distance_weight = 5

# Adjust as needed
# Example: Make price more important
price_weight = 15  # Increased from 10
```

## Benefits

✅ **Fair Ranking**: Considers multiple factors, not just one  
✅ **Patient-Centric**: Prioritizes availability and speed  
✅ **Transparent**: Ranking scores are included in responses  
✅ **Flexible**: Can adjust weights based on business needs  
✅ **Automatic**: No manual intervention needed  
