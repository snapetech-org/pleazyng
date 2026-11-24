part of 'publication_sync.dart';

typedef _RecoveryDocuments = ({SyncDocument publication, SyncDocument? discovery});

Future<String> _sealRecovery(SyncCipher cipher, String code, _RecoveryDocuments documents) async {
  final name = PublicationSync.recoveryName(cipher.namespace);
  final bundle = <String, dynamic>{
    'format': 'plezyng-encrypted-recovery-v2',
    'name': name,
    'encrypted': await cipher.seal(documents.publication, name),
  };
  if (documents.discovery case final discovery?) {
    final name = DiscoverySync.recoveryName(DiscoverySync.namespaceFor(code));
    bundle['discoveryName'] = name;
    bundle['discoveryEncrypted'] = await cipher.seal(discovery, name);
  }
  return canonicalSyncJson(bundle);
}

Future<_RecoveryDocuments> _openRecovery(String text, SyncCipher cipher, PublicationSyncConfig config) async {
  try {
    if (text.length > PublicationSync.maxRecoveryBytes || utf8.encode(text).length > PublicationSync.maxRecoveryBytes) {
      throw const SourceException(SourceFailure.invalidData);
    }
    final envelope = sourceMap(jsonDecode(text));
    final name = PublicationSync.recoveryName(cipher.namespace);
    final format = envelope['format'];
    if (!const {'plezyng-encrypted-recovery-v1', 'plezyng-encrypted-recovery-v2'}.contains(format) ||
        envelope['name'] != name ||
        envelope['encrypted'] is! String) {
      throw const SourceException(SourceFailure.invalidData);
    }
    final publication = await cipher.open(envelope['encrypted'] as String, name);
    SyncDocument? discovery;
    if (config.includeDiscovery &&
        format == 'plezyng-encrypted-recovery-v2' &&
        (envelope.containsKey('discoveryName') || envelope.containsKey('discoveryEncrypted'))) {
      final discoveryName = DiscoverySync.recoveryName(DiscoverySync.namespaceFor(config.code));
      if (envelope['discoveryName'] != discoveryName || envelope['discoveryEncrypted'] is! String) {
        throw const SourceException(SourceFailure.invalidData);
      }
      discovery = await cipher.open(envelope['discoveryEncrypted'] as String, discoveryName);
    }
    return (publication: publication, discovery: discovery);
  } on SourceException {
    rethrow;
  } on FormatException {
    throw const SourceException(SourceFailure.invalidData);
  }
}
