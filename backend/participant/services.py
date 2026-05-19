from django.db import transaction
from rest_framework.exceptions import ValidationError
from .models import Team, TeamMember, TeamRequest, ParticipantProfile


def add_member_to_team(team: Team, *, user=None, name=None, email=None, college=None, semester=None, degree=None, copy_skills_from_user=False):
    """
    Atomically add a member to `team`, enforcing capacity and auto-deleting pending invites when team fills.

    Raises `rest_framework.exceptions.ValidationError` on capacity or membership conflicts.
    Returns the `TeamMember` instance.
    """
    with transaction.atomic():
        # lock team row to avoid race conditions
        team = Team.objects.select_for_update().get(pk=team.pk)

        # Re-check capacity (leader + accepted members)
        if 1 + team.members.count() >= team.hackathon.max_team_size:
            raise ValidationError({"detail": "This team is already full."})

        # Verify user/email is not already in another team for this hackathon
        if user is not None:
            if Team.objects.filter(leader=user, hackathon=team.hackathon).exists():
                raise ValidationError({"detail": "You are already a leader of a team in this hackathon."})
            if TeamMember.objects.filter(email=user.email, team__hackathon=team.hackathon).exists():
                raise ValidationError({"detail": "You are already a member of a team in this hackathon."})

        # Ensure email not duplicated in same team (TeamMember.unique_together enforces it at DB level)
        member_kwargs = {
            'team': team,
            'email': email,
        }
        defaults = {
            'name': name or (user.get_full_name() if user else email),
            'college': college or 'Not Specified',
            'semester': semester or 1,
            'degree': degree or 'Not Specified',
        }

        member, created = TeamMember.objects.get_or_create(team=team, email=email, defaults=defaults)

        # Optionally copy skills from user's profile
        if copy_skills_from_user and user is not None and hasattr(user, 'participant_profile'):
            try:
                profile = user.participant_profile
                for skill in profile.skills.all():
                    member.skills.add(skill)
            except ParticipantProfile.DoesNotExist:
                pass

        # If team reached max capacity after this create, delete all pending invites
        if 1 + team.members.count() >= team.hackathon.max_team_size:
            TeamRequest.objects.filter(team=team, status='pending').delete()

        return member
