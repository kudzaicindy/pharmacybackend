# Alternative Medicine Suggestions - Implementation Details

## Current Implementation

The alternative suggestion system uses a **three-tier approach** for maximum reliability:

### 1. **AI-Powered Suggestions (Primary Method)**
- Uses Google Gemini to analyze the unavailable medicine and patient symptoms
- Generates context-aware alternatives based on:
  - Therapeutic equivalence
  - Safety considerations
  - Availability in Zimbabwe
  - Symptom matching
- Returns 2-3 most relevant alternatives

**Advantages:**
- Context-aware and intelligent
- Can handle uncommon medicines
- Considers patient symptoms
- Provides medically sound suggestions

**Example:**
```
Input: Medicine="amoxicillin", Symptoms=["sore throat", "fever"]
AI Output: ["azithromycin", "penicillin", "cephalexin"]
```

### 2. **Therapeutic Category Matching (Fallback)**
- Hardcoded dictionary of medicines grouped by therapeutic category
- Categories include:
  - Pain relievers (paracetamol, ibuprofen, aspirin)
  - Antibiotics (amoxicillin, penicillin, azithromycin)
  - Antacids, Cough medicines, Antihistamines, etc.
- Matches unavailable medicine to its category and returns alternatives

**Advantages:**
- Fast and reliable
- No API calls needed
- Works offline
- Covers common medicines

### 3. **Symptom-Based Matching (Last Resort)**
- If medicine not found in categories, uses symptoms to suggest
- Maps common symptoms to appropriate medicine categories
- Example: "headache" → suggests pain relievers

**Advantages:**
- Handles cases where medicine name is unknown
- Patient-centric approach
- Useful for symptom-based searches

## How It Works

```python
def suggest_alternatives(unavailable_medicine, symptoms):
    # Try AI first
    try:
        ai_suggestions = ask_gemini(medicine, symptoms)
        if ai_suggestions:
            return ai_suggestions
    except:
        pass
    
    # Fall back to category matching
    if medicine in therapeutic_categories:
        return therapeutic_categories[medicine]
    
    # Last resort: symptom-based
    if symptoms:
        return symptom_to_medicines(symptoms)
    
    return []
```

## Future Enhancements

According to your project document, you mentioned integrating:

1. **DrugBank API** - Comprehensive drug interaction database
   - Would provide more accurate therapeutic alternatives
   - Includes drug interaction checking
   - Professional-grade data

2. **Drug Interaction Database** - Safety checking
   - Prevents suggesting incompatible alternatives
   - Checks for contraindications
   - Validates patient safety

3. **Pharmacy Inventory Integration** - Real-time availability
   - Only suggests alternatives that are actually in stock
   - Checks multiple pharmacies
   - Provides availability status

## API Usage

```bash
POST /api/chatbot/alternatives/
{
  "medicine": "paracetamol",
  "symptoms": ["headache", "fever"]
}

Response:
{
  "unavailable_medicine": "paracetamol",
  "alternatives": ["ibuprofen", "aspirin", "diclofenac"]
}
```

## Current Limitations

1. **Limited Medicine Database**: Only covers common medicines
2. **No Drug Interaction Checking**: Doesn't verify safety of combinations
3. **No Real-time Availability**: Doesn't check if alternatives are in stock
4. **Basic Symptom Matching**: Simple keyword matching, not medical diagnosis

## Recommended Next Steps

1. Integrate DrugBank API for comprehensive drug data
2. Add drug interaction checking before suggesting alternatives
3. Query pharmacy inventories to verify alternative availability
4. Implement confidence scoring for suggestions
5. Add patient medical history consideration (allergies, conditions)
