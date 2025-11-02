"""
CATS MCP Server - Email Templates
Reusable email templates for recruiting workflows
"""
from __future__ import annotations

from typing import Any, Optional
from fastmcp import FastMCP


def register_email_templates(mcp: FastMCP) -> None:
    """Register email templates as resources"""

    @mcp.resource("template://email/rejection")
    async def rejection_email_template() -> str:
        """Professional rejection email template"""
        return """Subject: Update on Your Application for [POSITION_TITLE]

Dear [CANDIDATE_NAME],

Thank you for taking the time to apply for the [POSITION_TITLE] position at [COMPANY_NAME] and for speaking with our team. We appreciate your interest in joining our organization and the effort you put into the interview process.

After careful consideration, we have decided to move forward with other candidates whose qualifications more closely align with the specific needs of this role at this time. This was a difficult decision, as we were impressed by [SPECIFIC_POSITIVE_FEEDBACK].

We encourage you to keep an eye on our careers page for future opportunities that may be a better fit for your background and skills. We will also keep your resume on file for [TIME_PERIOD] and will reach out if a suitable position opens up.

Thank you again for your interest in [COMPANY_NAME]. We wish you the very best in your job search and future career endeavors.

Best regards,
[RECRUITER_NAME]
[TITLE]
[COMPANY_NAME]
"""

    @mcp.resource("template://email/interview-invitation")
    async def interview_invitation_template() -> str:
        """Interview invitation email template"""
        return """Subject: Interview Invitation for [POSITION_TITLE] at [COMPANY_NAME]

Dear [CANDIDATE_NAME],

Thank you for your interest in the [POSITION_TITLE] position at [COMPANY_NAME]. We were impressed by your qualifications and would like to invite you to interview with our team.

INTERVIEW DETAILS:
- Date: [DATE]
- Time: [TIME] [TIMEZONE]
- Duration: Approximately [DURATION] minutes
- Format: [VIDEO_CALL/IN_PERSON/PHONE]
- Location/Link: [MEETING_LINK or ADDRESS]

WHO YOU'LL MEET:
[INTERVIEWER_NAMES and TITLES]

WHAT TO PREPARE:
- Please review [MATERIALS_TO_REVIEW]
- Bring [ITEMS_TO_BRING] if applicable
- Be prepared to discuss [TOPICS]

Please confirm your availability by replying to this email. If this time doesn't work for you, please let me know and we'll find an alternative.

If you have any questions before the interview, feel free to reach out. We're looking forward to speaking with you!

Best regards,
[RECRUITER_NAME]
[TITLE]
[COMPANY_NAME]
[PHONE]
[EMAIL]
"""

    @mcp.resource("template://email/offer-letter")
    async def offer_letter_template() -> str:
        """Job offer letter template"""
        return """Subject: Job Offer - [POSITION_TITLE] at [COMPANY_NAME]

Dear [CANDIDATE_NAME],

We are pleased to offer you the position of [POSITION_TITLE] at [COMPANY_NAME]. We were very impressed with your qualifications and believe you will be a valuable addition to our team.

POSITION DETAILS:
- Title: [POSITION_TITLE]
- Department: [DEPARTMENT]
- Reports to: [MANAGER_NAME], [MANAGER_TITLE]
- Start Date: [START_DATE]
- Location: [LOCATION]

COMPENSATION & BENEFITS:
- Base Salary: $[SALARY] per [YEAR/HOUR]
- Bonus/Commission: [BONUS_STRUCTURE]
- Benefits: [BENEFITS_SUMMARY]
  * Health, Dental, Vision Insurance
  * 401(k) with [X]% company match
  * [PTO_DAYS] days PTO
  * [OTHER_BENEFITS]

NEXT STEPS:
1. Please review the attached formal offer letter and employment agreement
2. If you accept this offer, please sign and return by [DEADLINE_DATE]
3. Complete the attached new hire forms
4. Contact [HR_CONTACT] at [HR_EMAIL] with any questions

This offer is contingent upon:
- Successful completion of background check
- [OTHER_CONTINGENCIES]

We're excited about the possibility of you joining our team! Please don't hesitate to reach out if you have any questions.

Congratulations and we look forward to working with you!

Best regards,
[HIRING_MANAGER_NAME]
[TITLE]
[COMPANY_NAME]
"""

    @mcp.resource("template://email/follow-up")
    async def follow_up_template() -> str:
        """General candidate follow-up template"""
        return """Subject: Following Up - [POSITION_TITLE] Application

Hi [CANDIDATE_NAME],

I hope this email finds you well. I wanted to follow up regarding your application for the [POSITION_TITLE] position at [COMPANY_NAME].

[CONTEXT - Choose one:
- We're still in the process of reviewing applications and wanted to keep you updated on our timeline.
- We'd like to schedule the next step in our interview process.
- We need some additional information from you to move forward.
]

[SPECIFIC_MESSAGE]

[ACTION_ITEM - Choose one:
- Please let me know your availability for [NEXT_STEP].
- Could you provide [REQUESTED_INFORMATION] by [DEADLINE]?
- We'll be making our final decision by [DATE] and will reach out then.
]

Thank you for your patience and continued interest in this opportunity. Please feel free to reach out if you have any questions.

Best regards,
[RECRUITER_NAME]
[TITLE]
[COMPANY_NAME]
[PHONE]
[EMAIL]
"""

    @mcp.resource("template://email/reference-check")
    async def reference_check_template() -> str:
        """Reference check request template"""
        return """Subject: Reference Check for [CANDIDATE_NAME]

Dear [REFERENCE_NAME],

My name is [RECRUITER_NAME] and I'm reaching out from [COMPANY_NAME]. [CANDIDATE_NAME] has applied for the position of [POSITION_TITLE] with our organization and has listed you as a professional reference.

We would greatly appreciate 10-15 minutes of your time for a brief reference check. We'd like to learn more about [CANDIDATE_NAME]'s:

- Professional capabilities and work ethic
- Strengths and areas for development
- Working style and team collaboration
- Overall performance in their role with you

Would you be available for a quick phone call at your convenience? Please let me know a few times that work well for you, or feel free to call me directly at [PHONE].

Alternatively, if you prefer, I can send you a brief questionnaire to complete at your convenience.

All information shared will be kept confidential and used solely for employment consideration purposes.

Thank you in advance for your time and assistance!

Best regards,
[RECRUITER_NAME]
[TITLE]
[COMPANY_NAME]
[PHONE]
[EMAIL]
"""
