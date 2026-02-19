"""
Tests for response_helpers module - lazy loading and response optimization
"""
import pytest
from response_helpers import summarize_list_response


def test_summarize_with_embedded_response():
    """Test summarizing a CATS API response with _embedded format"""
    raw = {
        "_embedded": {
            "candidates": [
                {"id": 1, "first_name": "John", "last_name": "Doe", "email": "john@test.com",
                 "status": "active", "created_date": "2025-01-01", "resume_text": "Very long resume...",
                 "applications": [{"id": 100}], "interviews": [{"id": 200}]},
                {"id": 2, "first_name": "Jane", "last_name": "Smith", "email": "jane@test.com",
                 "status": "hired", "created_date": "2025-02-01", "resume_text": "Another long resume...",
                 "applications": [], "interviews": []},
            ]
        },
        "total": 50,
        "page": 1,
        "per_page": 10,
    }

    result = summarize_list_response(raw, "candidates")

    assert result["count"] == 2
    assert result["total"] == 50
    assert result["has_more"] is True
    assert result["next_page"] == 2
    assert "hint" in result

    # Verify only summary fields are included
    for candidate in result["candidates"]:
        assert "id" in candidate
        assert "first_name" in candidate
        assert "last_name" in candidate
        assert "email" in candidate
        # These large fields should NOT be in summary
        assert "resume_text" not in candidate
        assert "applications" not in candidate
        assert "interviews" not in candidate


def test_summarize_with_custom_fields():
    """Test summarizing with custom field selection"""
    raw = {
        "_embedded": {
            "candidates": [
                {"id": 1, "first_name": "John", "last_name": "Doe", "email": "john@test.com",
                 "phone": "555-1234", "status": "active"},
            ]
        },
        "total": 1,
        "page": 1,
        "per_page": 10,
    }

    result = summarize_list_response(raw, "candidates", fields="id,first_name,phone")

    candidate = result["candidates"][0]
    assert candidate["id"] == 1
    assert candidate["first_name"] == "John"
    assert candidate["phone"] == "555-1234"
    assert "last_name" not in candidate
    assert "email" not in candidate


def test_summarize_no_more_pages():
    """Test response when there are no more pages"""
    raw = {
        "_embedded": {
            "jobs": [
                {"id": 1, "title": "Engineer", "status": "open"},
            ]
        },
        "total": 1,
        "page": 1,
        "per_page": 10,
    }

    result = summarize_list_response(raw, "jobs")

    assert result["has_more"] is False
    assert "next_page" not in result or result.get("next_page") is None


def test_summarize_none_response():
    """Test handling of None response"""
    result = summarize_list_response(None, "candidates")

    assert result["error"] == "No response from API"
    assert result["items"] == []
    assert result["total"] == 0


def test_summarize_jobs_fields():
    """Test job summary uses correct default fields"""
    raw = {
        "_embedded": {
            "jobs": [
                {"id": 1, "title": "Engineer", "status": "open", "department": "Eng",
                 "location": "NYC", "created_date": "2025-01-01",
                 "description": "Very long job description...", "requirements": "Also long..."},
            ]
        },
        "total": 1,
        "page": 1,
        "per_page": 10,
    }

    result = summarize_list_response(raw, "jobs")

    job = result["jobs"][0]
    assert "id" in job
    assert "title" in job
    assert "status" in job
    # Large fields should be stripped
    assert "description" not in job
    assert "requirements" not in job


def test_summarize_pagination_hints():
    """Test pagination hint messages"""
    raw = {
        "_embedded": {
            "companies": [{"id": i, "name": f"Company {i}"} for i in range(10)]
        },
        "total": 100,
        "page": 3,
        "per_page": 10,
    }

    result = summarize_list_response(raw, "companies")

    assert result["has_more"] is True
    assert result["next_page"] == 4
    assert "page=4" in result["hint"]
    assert "get_companie" in result["hint"]  # singular form hint


def test_summarize_direct_list_format():
    """Test summarizing when items are directly in a key matching entity type"""
    raw = {
        "contacts": [
            {"id": 1, "first_name": "John", "last_name": "Doe", "email": "john@test.com"},
        ],
        "total": 1,
        "page": 1,
        "per_page": 10,
    }

    result = summarize_list_response(raw, "contacts")
    assert result["count"] == 1
    assert result["contacts"][0]["id"] == 1
