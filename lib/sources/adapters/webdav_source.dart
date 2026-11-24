import 'dart:convert';

import 'package:xml/xml.dart';

import '../source_models.dart';
import '../source_transport.dart';
import '../sync/sync_path.dart';

part 'webdav_state.dart';

typedef SourceCredentials = Future<Map<String, String>> Function();

class WebDavSourceAdapter extends SourceAdapter {
  WebDavSourceAdapter(super.source, super.transport, this.credentials);
  final SourceCredentials credentials;
  @override
  SourceCapabilities get capabilities {
    final hasStateFolder = normalizeSyncFolder(source.options['syncRoot']) != null;
    return SourceCapabilities(
      writableState: hasStateFolder,
      readingProgress: hasStateFolder,
      audioProgress: hasStateFolder,
      annotations: hasStateFolder,
    );
  }

  final _progressReads = Expando<_DavState>();
  Uri get root {
    final base = Uri.parse(source.endpoint.endsWith('/') ? source.endpoint : '${source.endpoint}/');
    return source.root.isEmpty ? base : base.resolve('${source.root.replaceAll(RegExp(r'^/+|/+$'), '')}/');
  }

  Uri contained(String path) {
    final uri = root.resolve(path);
    final base = root.pathSegments.where((s) => s.isNotEmpty).toList();
    final segments = uri.pathSegments;
    if (!sameSourceOrigin(root, uri) ||
        segments.any((s) => s == '..' || s == '.' || s.contains('/') || s.contains('\\')) ||
        segments.length < base.length ||
        List.generate(base.length, (i) => segments[i] == base[i]).contains(false)) {
      throw const SourceException(SourceFailure.permission);
    }
    return uri;
  }

  Future<Map<String, String>> headers() async {
    final c = await credentials();
    return {
      if (c['username']?.isNotEmpty == true || c['password']?.isNotEmpty == true)
        'Authorization': 'Basic ${base64Encode(utf8.encode('${c['username'] ?? ''}:${c['password'] ?? ''}'))}',
    };
  }

  @override
  Future<SourcePage> browse({String? parent, String? cursor, String? query}) async {
    final target = contained(parent ?? '');
    final response = await transport.text(
      'PROPFIND',
      target,
      headers: {...await headers(), 'Depth': '1', 'Content-Type': 'application/xml'},
      body:
          '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:displayname/><d:resourcetype/><d:getcontentlength/><d:getcontenttype/><d:getetag/></d:prop></d:propfind>',
    );
    final document = XmlDocument.parse(response);
    final result = <SourceEntry>[];
    for (final row in document.descendants.whereType<XmlElement>().where((e) => e.name.local == 'response')) {
      String? text(String tag) =>
          row.descendants.whereType<XmlElement>().where((e) => e.name.local == tag).firstOrNull?.innerText;
      final href = text('href');
      if (href == null) continue;
      final uri = contained(target.resolve(href).toString());
      if (uri.path.replaceAll(RegExp(r'/+$'), '') == target.path.replaceAll(RegExp(r'/+$'), '')) continue;
      final valid = row.descendants
          .whereType<XmlElement>()
          .where((e) => e.name.local == 'status')
          .any((e) => e.innerText.contains(' 200 '));
      if (!valid) continue;
      final folder = row.descendants.whereType<XmlElement>().any((e) => e.name.local == 'collection');
      final name = text('displayname') ?? uri.pathSegments.where((s) => s.isNotEmpty).last;
      if (query != null && !name.toLowerCase().contains(query.toLowerCase())) continue;
      final mime = text('getcontenttype') ?? '';
      final kind = folder ? SourceItemKind.folder : classifySourceFile(name, mime);
      if (kind == SourceItemKind.unknown) continue;
      final id = uri.toString();
      result.add(
        SourceEntry(
          sourceId: source.id,
          id: id,
          title: name,
          kind: kind,
          parentId: parent ?? '',
          files: folder
              ? []
              : [
                  PublicationFile(
                    id: id,
                    name: name,
                    mimeType: mime,
                    size: sourceInt(text('getcontentlength')),
                    revision: text('getetag'),
                  ),
                ],
        ),
      );
    }
    result.sort((a, b) => a.isFolder == b.isFolder ? a.title.compareTo(b.title) : (a.isFolder ? -1 : 1));
    return SourcePage(result);
  }

  @override
  Future<ContentLease> resolve(PublicationFile file) async => ContentLease(
    contained(file.id),
    headers: {...await headers(), if (file.revision?.startsWith('"') == true) 'If-Match': file.revision!},
    size: file.size,
    backgroundTransferSafe: true,
  );

  Uri _stateUri(SourceEntry entry, PublicationProgress progress) {
    final syncFolder = normalizeSyncFolder(source.options['syncRoot']);
    if (syncFolder == null) throw const SourceException(SourceFailure.unsupported);
    final folder = contained('${encodeSyncFolderPath(syncFolder)}/');
    return folder.resolve(
      '${sourceDigest(jsonEncode([entry.syncIdentity ?? entry.id, progress.edition, progress.mode.name]))}.json',
    );
  }

  @override
  Future<PublicationProgress?> readProgress(SourceEntry entry, PublicationProgress local) async {
    _progressReads[local] = null;
    final state = await _readState(_stateUri(entry, local));
    PublicationProgress? result;
    if (state.data['progress'] != null) {
      final remote = sourceMap(state.data['progress']);
      if (remote['edition'] != local.edition || remote['mode'] != local.mode.name) {
        throw const SourceException(SourceFailure.invalidData);
      }
      result = PublicationProgress.fromJson({...remote, 'entryKey': entry.key});
    }
    _progressReads[local] = state;
    return result;
  }

  @override
  Future<void> writeProgress(SourceEntry entry, PublicationProgress progress) async {
    final state = _progressReads[progress];
    if (state == null || state.uri != _stateUri(entry, progress)) throw const SourceException(SourceFailure.conflict);
    _progressReads[progress] = null;
    await _writeState(state, {...state.data, 'progress': progress.toJson()});
  }

  @override
  Future<List<BookAnnotation>> readAnnotations(SourceEntry entry, PublicationProgress progress) async {
    final uri = _stateUri(entry, progress), reviewed = _progressReads[progress];
    final state = reviewed != null && reviewed.uri == uri ? reviewed : await _readState(uri);
    return sourceList(state.data['annotations'])
        .map(sourceMap)
        .where((j) => j['edition'] == progress.edition && j['mode'] == progress.mode.name)
        .map((j) => BookAnnotation.fromJson({...j, 'entryKey': entry.key}))
        .toList();
  }

  @override
  Future<void> writeAnnotation(SourceEntry entry, BookAnnotation annotation) async {
    final progress = PublicationProgress(
      entryKey: entry.key,
      edition: annotation.edition,
      mode: annotation.mode,
      session: '',
    );
    final uri = _stateUri(entry, progress);
    for (var attempt = 0; attempt < 3; attempt++) {
      final state = await _readState(uri);
      final notes = sourceList(state.data['annotations']).map(sourceMap).toList();
      final current = notes.where((n) => n['id'] == annotation.id).firstOrNull;
      if (current != null) {
        final updated = sourceInt(current['updatedAt']) ?? 0;
        if (updated > annotation.updatedAt || (updated == annotation.updatedAt && current['deleted'] == true)) return;
      }
      notes.removeWhere((n) => n['id'] == annotation.id);
      notes.add(annotation.toJson());
      try {
        await _writeState(state, {...state.data, 'annotations': notes});
        return;
      } on SourceException catch (e) {
        if (e.reason != SourceFailure.conflict || attempt == 2) rethrow;
      }
    }
  }
}
