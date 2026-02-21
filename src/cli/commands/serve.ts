/**
 * Serve command - Start HTTP API server for voice pipeline
 */

import chalk from 'chalk';
import { startServer } from '../../api/server';
import { logger } from '../../utils/logger';

export async function serveCommand(options: { port?: string }): Promise<void> {
  const port = parseInt(options.port || '3001', 10);

  console.log(chalk.blue(`Starting nano-claw API server on port ${port}...`));

  // Handle graceful shutdown
  const shutdown = () => {
    console.log(chalk.yellow('\nShutting down API server...'));
    process.exit(0);
  };

  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);

  try {
    await startServer(port);

    console.log(chalk.green(`✓ API server listening on http://localhost:${port}`));
    console.log(chalk.gray('  POST /api/chat          — send a message'));
    console.log(chalk.gray('  POST /api/chat/approve  — approve pending tools'));
    console.log(chalk.gray('  POST /api/chat/reject   — reject pending tools'));
    console.log(chalk.gray('  GET  /api/health        — health check'));
    console.log(chalk.gray('\nPress Ctrl+C to stop\n'));

    // Keep the process running
    await new Promise<never>(() => {});
  } catch (error) {
    logger.error('Serve command failed', error);
    console.error(chalk.red(`Error: ${(error as Error).message}`));
    process.exit(1);
  }
}
