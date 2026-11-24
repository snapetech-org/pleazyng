import 'dart:convert';

import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:plezy/sources/adapters/cloud_sources.dart';
import 'package:plezy/sources/adapters/opds_source.dart';
import 'package:plezy/sources/adapters/s3_source.dart';
import 'package:plezy/sources/adapters/webdav_source.dart';
import 'package:plezy/sources/source_database.dart';
import 'package:plezy/sources/source_models.dart';
import 'package:plezy/sources/source_transport.dart';

void main() {
  test('source transport decodes top-level JSON arrays', () async {
    final transport = SourceTransport(client: MockClient((_) async => http.Response('[{"type":"movie"}]', 200)));
    expect(await transport.jsonValue('GET', Uri.parse('https://catalog.example/search')), [
      {'type': 'movie'},
    ]);
    transport.close();
  });

  test('stream-only publication files round-trip without changing identity', () {
    const streamed = PublicationFile(
      id: 'stable-id',
      name: 'book.cbz',
      mimeType: 'application/vnd.comicbook+zip',
      revision: 'revision-1',
      size: 42,
      streamOnly: true,
    );
    const archived = PublicationFile(
      id: 'stable-id',
      name: 'book.cbz',
      mimeType: 'application/vnd.comicbook+zip',
      revision: 'revision-1',
      size: 42,
    );

    final restored = PublicationFile.fromJson(streamed.toJson());
    expect(restored.streamOnly, isTrue);
    expect(restored.fingerprint, archived.fingerprint);
    expect(PublicationFile.fromJson(archived.toJson()).streamOnly, isFalse);
    expect(archived.fingerprint, '7aa359edddc5b08b8c0bfc311d9c7e605c5a277f17db26c1c9b5d0827a0f226a');
  });

  test('cross-origin redirects strip authentication', () async {
    final requests = <http.Request>[];
    final transport = SourceTransport(
      client: MockClient((r) async {
        requests.add(r);
        return requests.length == 1
            ? http.Response('', 302, headers: {'location': 'https://cdn.example/file'})
            : http.Response('audio', 200);
      }),
    );
    await transport.text(
      'GET',
      Uri.parse('https://drive.example/file'),
      headers: {'Authorization': 'Bearer secret', 'Cookie': 'secret', 'Range': 'bytes=1-'},
      redirects: true,
    );
    expect(requests.last.headers.containsKey('Authorization'), isFalse);
    expect(requests.last.headers.containsKey('Cookie'), isFalse);
    expect(requests.last.headers['Range'], 'bytes=1-');
    transport.close();
  });
  test('HTTPS redirect cannot downgrade even when LAN HTTP is allowed', () async {
    final transport = SourceTransport(
      allowHttp: true,
      client: MockClient((r) async => http.Response('', 302, headers: {'location': 'http://example/file'})),
    );
    await expectLater(
      transport.text('GET', Uri.parse('https://example/file'), redirects: true),
      throwsA(isA<SourceException>()),
    );
    transport.close();
  });
  test('cached browsing recovers the same default and configured roots for every cloud drive', () async {
    final repository = SourceRepository(SourceDatabase(NativeDatabase.memory()));
    addTearDown(repository.database.close);
    for (final kind in [FileSourceKind.googleDrive, FileSourceKind.oneDrive, FileSourceKind.dropbox]) {
      for (final root in ['', kind == FileSourceKind.dropbox ? '/selected' : 'selected-id']) {
        final source = FileSource(id: '${kind.name}:$root', name: 'Drive', kind: kind, profileId: 'p', root: root);
        final transport = SourceTransport(
          client: MockClient(
            (_) async => http.Response(
              jsonEncode(switch (kind) {
                FileSourceKind.googleDrive => {
                  'files': [
                    {'id': 'book', 'name': 'Issue.cbz', 'mimeType': 'application/zip'},
                  ],
                },
                FileSourceKind.oneDrive => {
                  'value': [
                    {
                      'id': 'book',
                      'name': 'Issue.cbz',
                      'file': {'mimeType': 'application/zip'},
                    },
                  ],
                },
                _ => {
                  'entries': [
                    {'id': 'book', 'name': 'Issue.cbz', '.tag': 'file'},
                  ],
                },
              }),
              200,
            ),
          ),
        );
        Future<String> token({bool refresh = false}) async => 'fixture';
        final adapter = switch (kind) {
          FileSourceKind.googleDrive => GoogleDriveSourceAdapter(source, transport, token),
          FileSourceKind.oneDrive => OneDriveSourceAdapter(source, transport, token),
          _ => DropboxSourceAdapter(source, transport, token),
        };
        addTearDown(adapter.close);
        final rootEntries = (await adapter.browse()).entries;
        expect(rootEntries.single.title, 'Issue.cbz');
        await repository.cacheEntries('p', rootEntries);
        final other = SourceEntry(
          sourceId: source.id,
          id: 'nested',
          title: 'Other',
          kind: SourceItemKind.folder,
          parentId: 'some-child-folder',
        );
        await repository.cacheEntries('p', [other]);
        final restored = await repository.entriesPage(
          'p',
          source: source.id,
          parent: source.indexedRootParent,
          filterParent: true,
        );
        expect(restored.items.map((item) => item.id), ['book'], reason: '${kind.name}: $root');
      }
    }
  });

  test('OneDrive resolves fresh download URL without passing Graph credentials to CDN', () async {
    var calls = 0;
    final transport = SourceTransport(
      client: MockClient((r) async {
        calls++;
        expect(r.headers['Authorization'], 'Bearer private');
        return http.Response(
          jsonEncode({'@microsoft.graph.downloadUrl': 'https://cdn.example/signed-$calls', 'size': 12}),
          200,
        );
      }),
    );
    final source = OneDriveSourceAdapter(
      const FileSource(id: 'one', profileId: 'p', kind: FileSourceKind.oneDrive, name: 'One'),
      transport,
      ({bool refresh = false}) async => 'private',
    );
    const file = PublicationFile(id: 'item', name: 'book.m4b', mimeType: 'audio/mp4');
    final a = await source.resolve(file), b = await source.resolve(file);
    expect(a.headers, isEmpty);
    expect(b.headers, isEmpty);
    expect(a.uri, isNot(b.uri));
    source.close();
  });
  test('S3 directory roots scope every listing and reject sibling reads', () async {
    for (final root in ['books', 'books/']) {
      final source = S3SourceAdapter(
        FileSource(
          id: 's',
          profileId: 'p',
          kind: FileSourceKind.s3,
          name: 'S3',
          endpoint: 'https://store.example/bucket',
          root: root,
        ),
        SourceTransport(
          client: MockClient((request) async {
            expect(request.url.queryParameters['prefix'], 'books/');
            return http.Response(
              '<ListBucketResult><Contents><Key>books/Novel.epub</Key></Contents>'
              '<CommonPrefixes><Prefix>books-old/</Prefix></CommonPrefixes></ListBucketResult>',
              200,
            );
          }),
        ),
        () async => {'accessKey': 'fixture', 'secretKey': 'fixture'},
      );
      addTearDown(source.close);
      expect((await source.browse()).entries.single.title, 'Novel.epub');
      await expectLater(
        source.resolve(const PublicationFile(id: 'books-old/leak.mp3', name: 'leak.mp3', mimeType: 'audio/mpeg')),
        throwsA(isA<SourceException>().having((e) => e.reason, 'reason', SourceFailure.permission)),
      );
    }
  });

  test('Drive shortcuts index the target edition, retain folder identity and reject target changes', () async {
    const checksum = '610a88e326eaabdf6085fe642056c4bc';
    var revision = checksum;
    final source = GoogleDriveSourceAdapter(
      const FileSource(id: 'drive', profileId: 'me', name: 'Drive', kind: FileSourceKind.googleDrive),
      SourceTransport(
        client: MockClient((request) async {
          final target = {
            'id': 'audio',
            'name': 'Original.mp3',
            'mimeType': 'audio/mpeg',
            'size': '300',
            'md5Checksum': revision,
            'resourceKey': 'rk',
          };
          if (request.url.path.endsWith('/audio')) {
            expect(request.headers['X-Goog-Drive-Resource-Keys'], 'audio/rk');
            return http.Response(jsonEncode(target), 200);
          }
          return http.Response(
            jsonEncode({
              'files': [
                {
                  'id': 'alias',
                  'name': 'Shortcut.mp3',
                  'version': '1',
                  'mimeType': 'application/vnd.google-apps.shortcut',
                  'shortcutDetails': {'targetId': 'audio', 'targetMimeType': 'audio/mpeg', 'targetResourceKey': 'rk'},
                },
                target,
              ],
            }),
            200,
          );
        }),
      ),
      ({bool refresh = false}) async => 'fixture',
    );
    addTearDown(source.close);
    final entries = (await source.browse()).entries;
    final shortcut = entries.first.files.single;
    expect(entries.first.id, isNot(entries.last.id));
    expect(shortcut.fingerprint, entries.last.files.single.fingerprint);
    expect(shortcut.revision, checksum);
    expect((await source.resolve(shortcut)).size, 300);
    final oldFile = PublicationFile(id: shortcut.id, name: shortcut.name, mimeType: shortcut.mimeType, revision: '1');
    final oldEntry = SourceEntry(
      id: oldFile.id,
      sourceId: 'drive',
      title: oldFile.name,
      kind: SourceItemKind.music,
      parentId: 'root',
      files: [oldFile],
    );
    final repaired = await source.hydrate(oldEntry);
    expect(repaired.key, oldEntry.key);
    expect(repaired.files.single.fingerprint, shortcut.fingerprint);
    revision = 'different';
    await expectLater(
      source.resolve(shortcut),
      throwsA(isA<SourceException>().having((e) => e.reason, 'reason', SourceFailure.conflict)),
    );
  });

  test('Drive API 401 refreshes once and retries with new token', () async {
    var calls = 0;
    final refreshes = <bool>[];
    final source = GoogleDriveSourceAdapter(
      const FileSource(id: 'g', profileId: 'p', kind: FileSourceKind.googleDrive, name: 'Drive'),
      SourceTransport(
        client: MockClient((r) async {
          calls++;
          return calls == 1 ? http.Response('', 401) : http.Response('{"files":[]}', 200);
        }),
      ),
      ({bool refresh = false}) async {
        refreshes.add(refresh);
        return refresh ? 'new' : 'old';
      },
    );
    expect((await source.browse()).entries, isEmpty);
    expect(refreshes, [false, true]);
    source.close();
  });
  test('OPDS acquisition refresh never reuses a removed signed link', () async {
    var removed = false;
    final source = OpdsSourceAdapter(
      const FileSource(
        id: 'opds',
        profileId: 'p',
        kind: FileSourceKind.opds,
        name: 'Books',
        endpoint: 'https://catalog.example/feed',
      ),
      SourceTransport(
        client: MockClient(
          (r) async => http.Response(
            jsonEncode({
              'metadata': {'title': 'Catalog'},
              'publications': removed
                  ? []
                  : [
                      {
                        'metadata': {'title': 'Book', 'identifier': 'book-1'},
                        'links': [
                          {
                            'rel': 'http://opds-spec.org/acquisition/open-access',
                            'type': 'application/epub+zip',
                            'href': 'https://cdn.example/book?signature=private',
                          },
                        ],
                      },
                    ],
            }),
            200,
          ),
        ),
      ),
      () async => {'username': 'u', 'password': 'p'},
    );
    final page = await source.browse(), file = page.entries.single.files.single;
    expect(jsonEncode(page.entries.single.toJson()), isNot(contains('signature=private')));
    final lease = await source.resolve(file);
    expect(lease.headers, isEmpty);
    removed = true;
    await expectLater(source.resolve(file), throwsA(isA<SourceException>()));
    source.close();
  });
  test('OPDS does not treat borrowing or purchases as downloadable files', () async {
    final source = OpdsSourceAdapter(
      const FileSource(
        id: 'o',
        profileId: 'p',
        kind: FileSourceKind.opds,
        name: 'Books',
        endpoint: 'https://example/feed',
      ),
      SourceTransport(
        client: MockClient(
          (_) async => http.Response(
            jsonEncode({
              'publications': [
                {
                  'metadata': {'title': 'Loan', 'identifier': 'id'},
                  'links': [
                    {
                      'rel': 'http://opds-spec.org/acquisition/borrow',
                      'type': 'application/epub+zip',
                      'href': '/checkout',
                    },
                  ],
                },
              ],
            }),
            200,
          ),
        ),
      ),
      () async => {},
    );
    expect((await source.browse()).entries, isEmpty);
    source.close();
  });
  test('OPDS marks provider-declared samples as isolated previews', () async {
    final source = OpdsSourceAdapter(
      const FileSource(
        id: 'samples',
        profileId: 'p',
        kind: FileSourceKind.opds,
        name: 'Books',
        endpoint: 'https://catalog.example/feed',
      ),
      SourceTransport(
        client: MockClient(
          (_) async => http.Response(
            jsonEncode({
              'publications': [
                {
                  'metadata': {'title': 'Sampled book', 'identifier': '9780306406157'},
                  'links': [
                    {
                      'rel': 'http://opds-spec.org/acquisition http://opds-spec.org/acquisition/sample',
                      'type': 'application/epub+zip',
                      'href': '/book-sample.epub',
                    },
                    {
                      'rel': 'http://opds-spec.org/acquisition/open-access',
                      'type': 'application/epub+zip',
                      'href': '/book.epub',
                    },
                  ],
                },
              ],
            }),
            200,
          ),
        ),
      ),
      () async => {},
    );

    final files = (await source.browse()).entries.single.files;
    expect(files, hasLength(2));
    expect(files.first.sampleOnly, isTrue);
    expect(files.first.previewableSample, isTrue);
    expect(files.last.sampleOnly, isFalse);
    expect(files.first.fingerprint, isNot(files.last.fingerprint));
    expect(PublicationFile.fromJson(files.first.toJson()).sampleOnly, isTrue);
    expect(files.first.toJson(), containsPair('sampleOnly', true));
    expect(files.first.toJson(), isNot(contains('sample')));
    expect(
      PublicationFile.fromJson({'id': 'legacy', 'name': 'sample.epub', 'sample': true}).sampleOnly,
      isTrue,
      reason: 'cached data from the earlier sample schema remains preview-only',
    );
    expect((await source.browse()).entries.single.identifiers, containsPair('isbn13', '9780306406157'));
    source.close();
  });
  test('WebDAV rejects traversal and external origins', () {
    final source = WebDavSourceAdapter(
      const FileSource(
        id: 'w',
        profileId: 'p',
        kind: FileSourceKind.webdav,
        name: 'Books',
        endpoint: 'https://dav.example/books/',
      ),
      SourceTransport(),
      () async => {},
    );
    for (final path in [
      '../secret',
      'https://evil.example/books/x',
      '/books-other/file',
      '/books/%2e%2e/secret',
      '/books/a%2fb',
    ]) {
      expect(() => source.contained(path), throwsA(isA<SourceException>()));
    }
    expect(source.contained('novel.epub').path, '/books/novel.epub');
    source.close();
  });
  test('WebDAV state uses ETag and preserves unrelated annotations', () async {
    final requests = <http.Request>[];
    final source = WebDavSourceAdapter(
      const FileSource(
        id: 'w',
        profileId: 'p',
        kind: FileSourceKind.webdav,
        name: 'Books',
        endpoint: 'https://dav.example/books/',
        options: {'syncRoot': '.state'},
      ),
      SourceTransport(
        client: MockClient((r) async {
          requests.add(r);
          return r.method == 'GET'
              ? http.Response('{"version":1,"annotations":[{"id":"existing"}]}', 200, headers: {'etag': '"revision1"'})
              : http.Response('', 204);
        }),
      ),
      () async => {},
    );
    const entry = SourceEntry(
      sourceId: 'w',
      id: 'https://dav.example/books/book.epub',
      title: 'Book',
      kind: SourceItemKind.ebook,
    );
    final progress = PublicationProgress(
      entryKey: entry.key,
      edition: 'e',
      mode: PublicationMode.reading,
      session: 's',
      fraction: .4,
    );
    await source.readProgress(entry, progress);
    await source.writeProgress(entry, progress);
    expect(requests.map((request) => request.method), ['GET', 'PUT']);
    expect(requests.last.url.path, matches(RegExp(r'^/books/\.state/[a-f0-9]{64}\.json$')));
    expect(requests.last.url.path, isNot(contains('book.epub')));
    expect(requests.last.headers['If-Match'], '"revision1"');
    expect(jsonDecode(requests.last.body)['annotations'], hasLength(1));
    expect(requests.every((request) => request.url.path.startsWith('/books/.state/')), isTrue);
    expect(requests.every((request) => request.url.path.endsWith('.json')), isTrue);
    source.close();
  });

  group('WebDAV synchronized state', () {
    const entry = SourceEntry(sourceId: 'w', id: 'book', title: 'Book', kind: SourceItemKind.ebook);
    final progress = PublicationProgress(
      entryKey: entry.key,
      edition: 'edition',
      mode: PublicationMode.reading,
      session: 'session',
    );
    WebDavSourceAdapter adapter(MockClient client) {
      final source = WebDavSourceAdapter(
        const FileSource(
          id: 'w',
          profileId: 'p',
          kind: FileSourceKind.webdav,
          name: 'Books',
          endpoint: 'https://dav.example/books/',
          options: {'syncRoot': '.state'},
        ),
        SourceTransport(client: client),
        () async => {},
      );
      addTearDown(source.close);
      return source;
    }

    test('an overlapping reader cannot advance the precondition for an earlier write', () async {
      var revision = '"v1"';
      final writes = <String?>[];
      final source = adapter(
        MockClient((request) async {
          if (request.method == 'GET') {
            return http.Response('{"version":1}', 200, headers: {'etag': revision});
          }
          writes.add(request.headers['If-Match']);
          return http.Response('', writes.last == revision ? 204 : 412);
        }),
      );
      await source.readProgress(entry, progress);
      revision = '"v2"';
      final other = progress.copyWith(session: 'other');
      await source.readProgress(entry, other);
      await expectLater(
        source.writeProgress(entry, progress),
        throwsA(isA<SourceException>().having((e) => e.reason, 'reason', SourceFailure.conflict)),
      );
      await source.writeProgress(entry, other);
      expect(writes, ['"v1"', '"v2"']);
    });

    test('state updates require a single strong revision for an existing file', () async {
      for (final tag in ['W/"old"', 'unquoted', '*', '"one", "two"']) {
        final source = adapter(
          MockClient((request) async {
            expect(request.method, 'GET');
            return http.Response('{"version":1}', 200, headers: {'etag': tag});
          }),
        );
        await source.readProgress(entry, progress);
        await expectLater(
          source.writeProgress(entry, progress),
          throwsA(isA<SourceException>().having((e) => e.reason, 'reason', SourceFailure.unsupported)),
        );
      }
    });

    test('a note that would make the state unreadable leaves the remote copy intact', () async {
      final requests = <String>[];
      final source = adapter(
        MockClient((request) async {
          requests.add(request.method);
          return http.Response('', request.method == 'GET' ? 404 : 201);
        }),
      );
      final note = BookAnnotation(
        id: 'note',
        entryKey: entry.key,
        edition: progress.edition,
        mode: progress.mode,
        note: 'é' * (1024 * 1024),
        updatedAt: 1,
      );
      await expectLater(source.writeAnnotation(entry, note), throwsA(isA<SourceException>()));
      expect(requests, ['GET']);
    });
  });

  test('WebDAV refuses unsafe state folders before any request', () async {
    var requests = 0;
    const entry = SourceEntry(
      sourceId: 'w',
      id: 'https://dav.example/books/book.epub',
      title: 'Book',
      kind: SourceItemKind.ebook,
    );
    final progress = PublicationProgress(
      entryKey: entry.key,
      edition: 'e',
      mode: PublicationMode.reading,
      session: 's',
      fraction: .4,
    );
    for (final root in [
      '',
      '.',
      './',
      '/state',
      'state//nested',
      'folder/..',
      '%2e%2e',
      '%252e%252e',
      'folder%2f..',
      'folder%5c..',
      'broken%',
      r'folder\state',
    ]) {
      final source = WebDavSourceAdapter(
        FileSource(
          id: 'w',
          profileId: 'p',
          kind: FileSourceKind.webdav,
          name: 'Books',
          endpoint: 'https://dav.example/books/',
          options: {'syncRoot': root},
        ),
        SourceTransport(
          client: MockClient((request) async {
            requests++;
            return http.Response('', 200);
          }),
        ),
        () async => {},
      );
      expect(source.capabilities.writableState, isFalse, reason: 'unsafe syncRoot: $root');
      await expectLater(source.readProgress(entry, progress), throwsA(isA<SourceException>()));
      await expectLater(source.writeProgress(entry, progress), throwsA(isA<SourceException>()));
      source.close();
    }
    expect(requests, 0, reason: 'invalid WebDAV state folders must fail before touching the remote source');
  });
}
