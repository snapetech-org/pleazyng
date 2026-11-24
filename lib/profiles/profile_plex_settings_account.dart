import '../connection/connection_registry.dart';
import 'active_plex_identity.dart';
import 'active_plex_token.dart';
import 'profile.dart';
import 'profile_connection_registry.dart';

/// Resolves the credential and settings owner from the same profile connection.
Future<ActivePlexToken?> resolveProfilePlexSettingsAccount({
  required Profile profile,
  required ConnectionRegistry connections,
  required ProfileConnectionRegistry profileConnections,
}) async {
  if (profile.kind == ProfileKind.plexHome) {
    final parent = profile.parentConnectionId, user = profile.plexHomeUserUuid;
    if (parent == null || user == null || user.isEmpty) return null;
    final account = await connections.getPlexAccount(parent);
    if (account == null) return null;
    final link = await profileConnections.get(profile.id, parent);
    if (link == null || link.userIdentifier != user || !link.hasToken) return null;
    return ActivePlexToken(
      identity: ActivePlexIdentity(account: account, userUuid: user),
      token: link.userToken!,
    );
  }

  for (final link in await profileConnections.listForProfile(profile.id)) {
    final account = await connections.getPlexAccount(link.connectionId);
    if (account == null) continue;
    final user = link.userIdentifier.isEmpty ? null : link.userIdentifier;
    // A named link borrows a Plex Home user. Its token may be cleared for
    // reauthentication while the account owner's credential remains valid.
    final token = link.hasToken ? link.userToken : (user == null ? account.accountToken : null);
    if (token == null || token.isEmpty) return null;
    return ActivePlexToken(
      identity: ActivePlexIdentity(account: account, userUuid: user),
      token: token,
    );
  }
  return null;
}
