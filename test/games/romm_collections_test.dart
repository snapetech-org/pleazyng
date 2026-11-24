import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:plezy/games/romm/romm_client.dart';

void main() {
  test('RomM virtual collections include the required type=all query', () async {
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    addTearDown(() => server.close(force: true));
    final requests = <Uri>[];
    server.listen((request) async {
      requests.add(request.uri);
      request.response.headers.contentType = ContentType.json;
      if (request.uri.path == '/api/collections/virtual' && request.uri.queryParameters['type'] != 'all') {
        request.response.statusCode = HttpStatus.unprocessableEntity;
        request.response.write('{"detail":"Missing required query type"}');
      } else {
        request.response.write('[]');
      }
      await request.response.close();
    });

    final client = RommClient(Uri.parse('http://127.0.0.1:${server.port}/'));
    addTearDown(client.close);

    expect(await client.collections(), isEmpty);
    expect(requests.map((url) => url.path), ['/api/collections', '/api/collections/smart', '/api/collections/virtual']);
    expect(requests.last.queryParameters, containsPair('type', 'all'));
  });
}
