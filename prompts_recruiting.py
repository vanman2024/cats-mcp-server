"""
CATS MCP Server - Recruiting Prompts
Example prompts for common recruiting workflows
"""
from __future__ import annotations

from typing import Any, Optional, Callable, Awaitable
from fastmcp import FastMCP

# Type alias for make_request callable
MakeRequestCallable = Callable[[str, str, Optional[dict[str, Any]], Optional[dict[str, Any]]], Awaitable[dict[str, Any]]]


def register_recruiting_prompts(mcp: FastMCP, make_request: MakeRequestCallable) -> None:
    """Register recruiting-related prompts"""

    @mcp.prompt()
    async def draft_rejection_email(candidate_id: int) -> str:
        """Draft a professional rejection email for a candidate

        Args:
            candidate_id: The candidate's ID in CATS
        """
        # Fetch candidate details
        candidate = await make_request("GET", f"/candidates/{candidate_id}")

        prompt = f"""Draft a professional, empathetic rejection email for:

Candidate: {candidate.get('first_name')} {candidate.get('last_name')}
Email: {candidate.get('email')}
Applied for: {candidate.get('job_title', 'position')}

Requirements:
- Thank them for their time and interest
- Be respectful and encouraging
- Keep door open for future opportunities
- Professional but warm tone
- 3-4 paragraphs max
"""
        return prompt

    @mcp.prompt()
    async def screen_candidate_for_job(candidate_id: int, job_id: int) -> str:
        """Generate screening questions based on job requirements

        Args:
            candidate_id: The candidate's ID
            job_id: The job ID to screen for
        """
        # Fetch candidate and job details
        candidate = await make_request("GET", f"/candidates/{candidate_id}")
        job = await make_request("GET", f"/jobs/{job_id}")

        prompt = f"""Analyze this candidate against the job requirements and generate screening questions:

CANDIDATE: {candidate.get('first_name')} {candidate.get('last_name')}
- Current title: {candidate.get('title', 'N/A')}
- Experience: {candidate.get('work_history', [])}
- Skills: {candidate.get('skills', [])}

JOB: {job.get('title')}
- Requirements: {job.get('description', 'N/A')}
- Location: {job.get('location', 'N/A')}
- Salary range: {job.get('salary_range', 'N/A')}

Generate:
1. 5 screening questions targeting gaps between candidate experience and job requirements
2. Questions about their motivation for this specific role
3. Questions about availability and logistics
"""
        return prompt

    @mcp.prompt()
    async def write_job_description(job_title: str, department: str) -> str:
        """Generate a comprehensive job description template

        Args:
            job_title: The title of the position
            department: The department/team
        """
        prompt = f"""Create a comprehensive job description for:

Position: {job_title}
Department: {department}

Include:
1. Role Overview (2-3 sentences)
2. Key Responsibilities (5-7 bullet points)
3. Required Qualifications
   - Education
   - Experience (years)
   - Technical skills
   - Soft skills
4. Preferred Qualifications
5. Benefits & Perks
6. Company Culture section
7. Application Process

Make it engaging, inclusive, and SEO-friendly.
"""
        return prompt


def register_interview_prompts(mcp: FastMCP, make_request: MakeRequestCallable) -> None:
    """Register interview-related prompts"""

    @mcp.prompt()
    async def prepare_interview_guide(candidate_id: int, job_id: int, interview_type: str = "phone") -> str:
        """Generate an interview guide for a specific candidate and role

        Args:
            candidate_id: The candidate's ID
            job_id: The job ID they're interviewing for
            interview_type: Type of interview (phone, technical, behavioral, panel)
        """
        candidate = await make_request("GET", f"/candidates/{candidate_id}")
        job = await make_request("GET", f"/jobs/{job_id}")

        prompt = f"""Create a detailed {interview_type} interview guide:

CANDIDATE: {candidate.get('first_name')} {candidate.get('last_name')}
- Background: {candidate.get('title', 'N/A')}
- Resume highlights: {candidate.get('summary', 'N/A')}

ROLE: {job.get('title')}
- Key requirements: {job.get('description', 'N/A')[:300]}

Generate:
1. Opening (icebreaker questions)
2. 8-10 targeted questions based on:
   - Resume gaps or unclear items
   - Technical requirements for the role
   - Cultural fit
   - Motivation and career goals
3. Candidate questions to expect
4. Closing (next steps)
5. Red flags to watch for
6. Scorecard criteria
"""
        return prompt
