"""
CATS MCP Server - Candidate Resources
Exposes candidate/job data as readable resources
"""
from __future__ import annotations

from typing import Any, Optional, Callable, Awaitable
from fastmcp import FastMCP

# Type alias for make_request callable
MakeRequestCallable = Callable[[str, str, Optional[dict[str, Any]], Optional[dict[str, Any]]], Awaitable[dict[str, Any]]]


def register_candidate_resources(mcp: FastMCP, make_request: MakeRequestCallable) -> None:
    """Register candidate-related resources"""

    @mcp.resource("candidate://{candidate_id}")
    async def read_candidate_profile(candidate_id: int) -> str:
        """Read a candidate's full profile as formatted text

        Args:
            candidate_id: The candidate's ID in CATS

        Returns:
            Formatted candidate profile
        """
        candidate = await make_request("GET", f"/candidates/{candidate_id}")

        # Format as readable text for LLM consumption
        profile = f"""# Candidate Profile

## Basic Information
- Name: {candidate.get('first_name')} {candidate.get('last_name')}
- Email: {candidate.get('email')}
- Phone: {candidate.get('phone', 'N/A')}
- Location: {candidate.get('city', 'N/A')}, {candidate.get('state', 'N/A')}

## Professional Summary
{candidate.get('summary', 'No summary available')}

## Current Status
- Status: {candidate.get('status', 'Unknown')}
- Source: {candidate.get('source', 'Unknown')}
- Date Added: {candidate.get('date_created', 'N/A')}

## Work History
"""
        # Add work history if available
        for job in candidate.get('work_history', []):
            profile += f"\n### {job.get('title')} at {job.get('company')}\n"
            profile += f"- Duration: {job.get('start_date')} - {job.get('end_date', 'Present')}\n"
            profile += f"- {job.get('description', '')}\n"

        # Add education
        profile += "\n## Education\n"
        for edu in candidate.get('education', []):
            profile += f"- {edu.get('degree')} in {edu.get('major')} from {edu.get('school')}\n"

        # Add skills
        if candidate.get('skills'):
            profile += f"\n## Skills\n{', '.join(candidate.get('skills', []))}\n"

        return profile

    @mcp.resource("candidate://{candidate_id}/pipeline")
    async def read_candidate_pipeline(candidate_id: int) -> str:
        """Read a candidate's pipeline status across all jobs

        Args:
            candidate_id: The candidate's ID

        Returns:
            Formatted pipeline information
        """
        # Get candidate's pipeline entries
        pipelines = await make_request("GET", f"/candidates/{candidate_id}/pipelines")

        output = f"# Pipeline Status for Candidate {candidate_id}\n\n"

        for pipe in pipelines.get('items', []):
            output += f"""## {pipe.get('job_title', 'Unknown Job')}
- Stage: {pipe.get('stage', 'Unknown')}
- Status: {pipe.get('status', 'Unknown')}
- Added: {pipe.get('date_added', 'N/A')}
- Last Activity: {pipe.get('last_activity', 'N/A')}
- Rating: {pipe.get('rating', 'Not rated')}
- Notes: {pipe.get('notes', 'No notes')}

"""
        return output


def register_job_resources(mcp: FastMCP, make_request: MakeRequestCallable) -> None:
    """Register job-related resources"""

    @mcp.resource("job://{job_id}")
    async def read_job_description(job_id: int) -> str:
        """Read a job's full description

        Args:
            job_id: The job's ID in CATS

        Returns:
            Formatted job description
        """
        job = await make_request("GET", f"/jobs/{job_id}")

        description = f"""# Job: {job.get('title')}

## Overview
- Company: {job.get('company', 'N/A')}
- Department: {job.get('department', 'N/A')}
- Location: {job.get('city', 'N/A')}, {job.get('state', 'N/A')}
- Type: {job.get('type', 'N/A')}
- Status: {job.get('status', 'Unknown')}

## Salary & Compensation
- Salary Range: {job.get('salary_range', 'Not specified')}
- Duration: {job.get('duration', 'N/A')}

## Description
{job.get('description', 'No description available')}

## Requirements
{job.get('requirements', 'No requirements specified')}

## Statistics
- Total Candidates: {job.get('candidate_count', 0)}
- Date Posted: {job.get('date_created', 'N/A')}
"""
        return description

    @mcp.resource("job://{job_id}/candidates")
    async def read_job_candidates(job_id: int) -> str:
        """Read all candidates in a job's pipeline

        Args:
            job_id: The job's ID

        Returns:
            Formatted candidate list with pipeline stages
        """
        # Get all pipeline entries for this job
        pipelines = await make_request("GET", f"/jobs/{job_id}/pipelines")

        output = f"# Candidates for Job {job_id}\n\n"

        # Group by stage
        by_stage: dict[str, list] = {}
        for pipe in pipelines.get('items', []):
            stage = pipe.get('stage', 'Unknown')
            if stage not in by_stage:
                by_stage[stage] = []
            by_stage[stage].append(pipe)

        # Output by stage
        for stage, candidates in by_stage.items():
            output += f"## {stage} ({len(candidates)} candidates)\n\n"
            for cand in candidates:
                output += f"""- **{cand.get('candidate_name')}** (ID: {cand.get('candidate_id')})
  - Status: {cand.get('status')}
  - Rating: {cand.get('rating', 'Not rated')}
  - Last activity: {cand.get('last_activity', 'N/A')}

"""
        return output
