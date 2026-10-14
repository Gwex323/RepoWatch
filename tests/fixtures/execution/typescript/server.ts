import { createServer } from 'node:http';

const message: string = 'repowatch-typescript-fixture';
createServer((_request, response) => response.end(message)).listen(3000, '0.0.0.0');
