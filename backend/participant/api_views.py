from rest_framework import generics, viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import PermissionDenied
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Case, When, F, Value, BooleanField, Q
from django.utils import timezone

from organizer.models import Hackathon, ProblemStatement
from .models import Team, TeamMember, ParticipantProfile, Skill, TeamRequest
from .api_serializers import (
    TeamSerializer, TeamMemberSerializer,
    ParticipantDiscoverySerializer, JoinTeamSerializer,
    SelectProblemStatementSerializer, ParticipantProblemStatementSerializer,
    TeamRequestSerializer, ParticipantProfileSerializer
)


class ParticipantDiscoveryAPIView(generics.ListAPIView):
    """
    Search for participants who are looking for a team and have specific skills.
    Query params: ?skill=react&hackathon_id=1
    """
    serializer_class = ParticipantDiscoverySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        # Only visible profiles — never include the requesting user themselves
        queryset = ParticipantProfile.objects.filter(visibility=True).exclude(
            user=self.request.user
        )

        skill_query = self.request.query_params.get('skill')
        if skill_query:
            queryset = queryset.filter(skills__name__icontains=skill_query)

        hackathon_id = self.request.query_params.get('hackathon_id')
        if hackathon_id:
            # Exclude users who are leaders of teams in this hackathon
            leader_ids = Team.objects.filter(hackathon_id=hackathon_id).values_list('leader_id', flat=True)
            # Exclude users who are members of teams in this hackathon (by email)
            member_emails = TeamMember.objects.filter(team__hackathon_id=hackathon_id).values_list('email', flat=True)
            
            queryset = queryset.exclude(
                Q(user_id__in=leader_ids) | Q(user__email__in=member_emails)
            )

        return queryset.select_related('user').prefetch_related('skills').distinct()


class TeamViewSet(viewsets.ModelViewSet):
    serializer_class = TeamSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        # Team leader OR a member matched by email (TeamMember has no user FK)
        return Team.objects.filter(
            Q(leader=self.request.user) |
            Q(members__email=self.request.user.email)
        ).distinct()

    @action(detail=True, methods=['post'])
    def select_problem_statement(self, request, pk=None):
        """
        Concurrency-safe problem statement selection.
        Uses select_for_update() to acquire a row lock, then checks capacity.
        Once a team selects, it is locked in permanently (D-01).
        """
        team = self.get_object()

        # Only the team leader can select
        if team.leader != request.user:
            raise PermissionDenied("Only the team leader can select a problem statement.")

        # D-01: Selection is permanent
        if team.selected_problem_statement is not None:
            return Response(
                {"detail": "Problem statement is already locked in and cannot be changed."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = SelectProblemStatementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ps_id = serializer.validated_data['problem_statement_id']

        with transaction.atomic():
            try:
                ps = ProblemStatement.objects.select_for_update().get(
                    id=ps_id, hackathon=team.hackathon, is_active=True
                )
            except ProblemStatement.DoesNotExist:
                return Response(
                    {"detail": "Problem statement not found or inactive."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # D-02 + D-03: Check capacity under lock
            current_count = ps.selected_by_teams.count()
            if current_count >= ps.max_teams_allowed:
                return Response(
                    {"detail": "This problem statement has reached its capacity limit."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            team.selected_problem_statement = ps
            team.save(update_fields=['selected_problem_statement'])

            # Invalidate the cached problem statement list for this hackathon
            cache_key = f"problem_statements_list_{team.hackathon.id}"
            cache.delete(cache_key)

        return Response(
            {"detail": "Problem statement selected successfully."},
            status=status.HTTP_200_OK,
        )


class TeamMemberViewSet(viewsets.ModelViewSet):
    serializer_class = TeamMemberSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        # Users can only see members of teams they lead or belong to (matched by email)
        return TeamMember.objects.filter(
            Q(team__leader=self.request.user) |
            Q(team__members__email=self.request.user.email)
        ).distinct()

    def perform_create(self, serializer):
        team = serializer.validated_data['team']
        if team.leader != self.request.user:
            raise PermissionDenied("Only the team leader can add members.")
        
        # Enforce hackathon_id from team
        serializer.save(hackathon=team.hackathon)


class JoinTeamAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = JoinTeamSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = serializer.validated_data['invite_token']

        try:
            team = Team.objects.get(invite_token=token)
        except Team.DoesNotExist:
            return Response({"detail": "Invalid invite token."}, status=status.HTTP_404_NOT_FOUND)

        # Check if hackathon registration is still open (invites expire when registration closes)
        if timezone.now() > team.hackathon.registration_deadline:
            return Response({"detail": "Invite has expired because hackathon registration is closed."}, status=status.HTTP_400_BAD_REQUEST)

        # Check if user is already in the team
        if TeamMember.objects.filter(team=team, user=request.user).exists():
            return Response({"detail": "You are already a member of this team."}, status=status.HTTP_400_BAD_REQUEST)

        # Check if user is already in ANY team for this hackathon
        if TeamMember.objects.filter(hackathon=team.hackathon, user=request.user).exists():
            return Response({"detail": "You are already in a team for this hackathon."}, status=status.HTTP_400_BAD_REQUEST)

        TeamMember.objects.create(
            team=team,
            hackathon=team.hackathon,
            user=request.user,
            member_role='Member'
        )
        return Response({"detail": "Successfully joined team."}, status=status.HTTP_200_OK)


class ParticipantProblemStatementViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only endpoint for participants to browse problem statements.
    Implements Cache-Aside pattern:
      - Reads check cache first (sub-millisecond).
      - On cache miss, fetches from DB, annotates with capacity metrics, caches result.
      - Cache is invalidated by TeamViewSet.select_problem_statement on writes.
    """
    serializer_class = ParticipantProblemStatementSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return ProblemStatement.objects.filter(is_active=True).annotate(
            current_teams_count=Count('selected_by_teams'),
            is_full=Case(
                When(current_teams_count__gte=F('max_teams_allowed'), then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            ),
        )

    def list(self, request, *args, **kwargs):
        hackathon_id = request.query_params.get('hackathon_id')
        if not hackathon_id:
            return Response(
                {"detail": "hackathon_id query parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cache_key = f"problem_statements_list_{hackathon_id}"
        cached_data = cache.get(cache_key)

        if cached_data is not None:
            return Response(cached_data)

        # Cache miss — fetch from DB, annotate, serialize, cache
        queryset = self.get_queryset().filter(hackathon_id=hackathon_id)
        serializer = self.get_serializer(queryset, many=True)
        cache.set(cache_key, serializer.data, timeout=3600)
        return Response(serializer.data)


class TeamRequestViewSet(viewsets.ModelViewSet):
    serializer_class = TeamRequestSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        
        # Exclude requests for hackathons where the receiver is already in a team (leader or member)
        joined_hackathon_ids = list(Team.objects.filter(leader=user).values_list('hackathon_id', flat=True))
        joined_hackathon_ids += list(TeamMember.objects.filter(email=user.email).values_list('team__hackathon_id', flat=True))
        
        return TeamRequest.objects.filter(
            Q(receiver=user) | Q(team__leader=user)
        ).exclude(
            receiver=user,
            status='pending',
            team__hackathon_id__in=joined_hackathon_ids
        ).distinct()

    def perform_create(self, serializer):
        team = serializer.validated_data['team']
        receiver = serializer.validated_data['receiver']
        
        if team.leader != self.request.user:
            raise PermissionDenied("Only the team leader can send requests.")

        # Ensure team is not full (including pending invitations)
        from rest_framework import serializers
        if team.occupied_slots >= team.hackathon.max_team_size:
            raise serializers.ValidationError("Your team is full (including pending invitations).")
            
        # Ensure the receiver is actually visible
        try:
            if not receiver.participant_profile.visibility:
                raise serializers.ValidationError({"receiver": "This user is not currently accepting team requests."})
        except ParticipantProfile.DoesNotExist:
            raise serializers.ValidationError({"receiver": "This user does not have a participant profile."})
        
        serializer.save()

    @action(detail=True, methods=['post'])
    def accept(self, request, pk=None):
        team_request = self.get_object()
        if team_request.receiver != request.user:
            raise PermissionDenied("You can only accept requests sent to you.")
        
        if team_request.status != 'pending':
            return Response({"detail": "Request already processed."}, status=status.HTTP_400_BAD_REQUEST)

        team = team_request.team
        hackathon = team.hackathon

        # Check if the team is already full of accepted members
        if 1 + team.members.count() >= hackathon.max_team_size:
            return Response({"detail": "This team is already full."}, status=status.HTTP_400_BAD_REQUEST)

        # Ensure the receiver is not already in a team (leader or member) for this hackathon
        is_in_team = (
            Team.objects.filter(leader=request.user, hackathon=hackathon).exists() or
            TeamMember.objects.filter(email=request.user.email, team__hackathon=hackathon).exists()
        )
        if is_in_team:
            return Response({"detail": "You are already in a team for this hackathon."}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            team_request.status = 'accepted'
            team_request.save()
            
            # Get receiver's profile details to copy into TeamMember
            try:
                profile = request.user.participant_profile
                college = profile.college
                semester = profile.semester
                degree = profile.degree
            except Exception:
                college = 'Not Specified'
                semester = 1
                degree = 'Not Specified'

            # Create TeamMember
            member, _ = TeamMember.objects.get_or_create(
                team=team_request.team,
                email=request.user.email,
                defaults={
                    'name': request.user.get_full_name() or request.user.email,
                    'college': college,
                    'semester': semester,
                    'degree': degree
                }
            )
            # Copy receiver's skills if they exist
            if hasattr(request.user, 'participant_profile'):
                for skill in request.user.participant_profile.skills.all():
                    member.skills.add(skill)

        return Response({"detail": "Request accepted."}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def decline(self, request, pk=None):
        team_request = self.get_object()
        if team_request.receiver != request.user:
            raise PermissionDenied("You can only decline requests sent to you.")
        
        if team_request.status != 'pending':
            return Response({"detail": "Request already processed."}, status=status.HTTP_400_BAD_REQUEST)

        team_request.status = 'declined'
        team_request.save()
        return Response({"detail": "Request declined."}, status=status.HTTP_200_OK)

class ParticipantProfileUpdateAPIView(generics.UpdateAPIView):
    serializer_class = ParticipantProfileSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        profile, _ = ParticipantProfile.objects.get_or_create(
            user=self.request.user,
            defaults={'college': 'Not Specified', 'semester': 1, 'degree': 'Not Specified'}
        )
        return profile

    def update(self, request, *args, **kwargs):
        # Team leaders cannot make themselves visible in the recruiting pool
        is_leader = Team.objects.filter(leader=request.user).exists()
        visibility_requested = request.data.get('visibility')
        if is_leader and visibility_requested is True:
            return Response(
                {"detail": "Team leaders cannot enable recruiting visibility."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().update(request, *args, **kwargs)
