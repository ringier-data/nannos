import { ZodOpenApiOperationObject, ZodOpenApiPathsObject } from 'zod-openapi';
import Router from '@koa/router';
import { Context, DefaultContext, DefaultState, Middleware, Next } from 'koa';
import { $ZodType } from 'zod/v4/core';
import * as z from 'zod';
import { Ajv2020 as Ajv, ValidateFunction, ValidationError } from 'ajv/dist/2020.js';
import * as addFormats from 'ajv-formats';
import _ from 'lodash';

export class OpenApiValidationError {
  constructor(
    public message: string,
    public input: unknown,
    public errors: ValidationError['errors']
  ) {}
}

type RouteSpec = Omit<ZodOpenApiOperationObject, 'responses'> & {
  responses?: ZodOpenApiOperationObject['responses'];
  responseType?: $ZodType;
  requestType?: $ZodType;
};

export class OpenApiRouter<StateT = DefaultState, ContextT = DefaultContext> {
  // This will hold all the path specifications, e.g., { '/users': { get: { ... } } }
  public readonly pathSpecs: ZodOpenApiPathsObject = {};
  constructor(private router: Router<StateT, ContextT>) {}

  /**
   * Registers a GET route.
   * @param path - The OpenAPI path format, e.g., /users/{id}
   * @param spec - The OpenAPI specification for this endpoint.
   * @param middleware - The Koa handler(s) for this endpoint.
   */
  get(path: string, spec: RouteSpec, ...middleware: Array<Middleware>) {
    this.registerRoute('get', path, spec, ...middleware);
    return this; // Allow chaining
  }

  /**
   * Registers a POST route.
   */
  post(path: string, spec: RouteSpec, ...middleware: Array<Middleware>) {
    this.registerRoute('post', path, spec, ...middleware);
    return this;
  }

  /**
   * Registers a PUT route.
   */
  put(path: string, spec: RouteSpec, ...middleware: Array<Middleware>) {
    this.registerRoute('put', path, spec, ...middleware);
    return this;
  }

  /**
   * Registers a DELETE route.
   */
  delete(path: string, spec: RouteSpec, ...middleware: Array<Middleware>) {
    this.registerRoute('delete', path, spec, ...middleware);
    return this;
  }

  /**
   * Private helper to perform the actual registration.
   */
  private registerRoute(
    method: 'get' | 'post' | 'put' | 'delete',
    path: string,
    spec: RouteSpec,
    ...middleware: Array<Middleware>
  ) {
    // Failed if path contains OpenAPI path
    if (path.includes('{') || path.includes('}')) {
      throw new Error(`Path "${path}" contains OpenAPI parameters. Use :id instead of {id}`);
    }
    path = path.startsWith('/') ? path : `/${path}`; // Ensure path starts with '/'

    if (spec.requestType && spec.requestBody) {
      throw new Error('Use either requestType or requestBody, not both.');
    }

    if (spec.requestType) {
      spec.requestBody = {
        content: {
          'application/json': {
            schema: spec.requestType,
          },
        },
      };
    }
    if (spec.requestBody?.content && Object.keys(spec.requestBody.content).length >= 2) {
      throw new Error(
        'We can only support schema for the request body at the moment in order to keep validation logic simple. If required, enhance useValidateRequest middleware.'
      );
    }
    if (!spec.responses && !spec.responseType) {
      spec.responseType = z.object(z.any()); // Default to an empty object if no response type is provided
    }
    spec.responses = spec.responses ?? {};
    if (!spec.responses['200']) {
      spec.responses['200'] = {
        description: 'Successful response',
        content: {
          'application/json': {
            schema: spec.responseType,
          },
        },
      };
    }

    const openAPIPath = path.replace(/:(\w+)/g, '{$1}'); // Convert Koa params :id to OpenAPI params {id}

    // 1. Store the OpenAPI specification
    if (!this.pathSpecs[openAPIPath]) {
      this.pathSpecs[openAPIPath] = {};
    }
    this.pathSpecs[openAPIPath][method] = spec as ZodOpenApiOperationObject;

    // Insert validation middleware as the penultimate item
    middleware.splice(-1, 0, useValidateRequest(this.pathSpecs[openAPIPath][method]));

    // 2. Register the route with the actual Koa router
    return this.router[method](path, ...middleware);
  }
}

const validate = (type: 'body' | 'query' | 'path', schema: ValidateFunction, data: unknown) => {
  const result = schema(data);
  if (!result || schema.errors) {
    if (schema.errors) {
      throw new OpenApiValidationError(`Failed ${type} validation`, data, schema.errors);
    } else {
      throw new OpenApiValidationError('Failed validation', data, []);
    }
  }
};

const ajv = new Ajv({ allErrors: true, strict: false });
addFormats.default.default(ajv);
/**
 * Middleware to validate incoming requests against the OpenAPI specification.
 * We use AJV to validate the request JSON schema instead of the underlying Zod schema
 * since the JSON representation is not 1:1 (e.g. Date is not natively representable in JSON,
 * we expect in our project an ISO8601 encoded string)
 * @param spec The OpenAPI operation specification.
 */
const useValidateRequest = (spec: ZodOpenApiOperationObject): Middleware => {
  const jsonSchemaOptions: Parameters<typeof z.toJSONSchema>[1] = {
    unrepresentable: 'any', // If we encounter a zod type that cannot be represented in JSON, we allow anything
    override: (ctx) => {
      const def = ctx.zodSchema._zod.def;
      if (def.type === 'date') {
        ctx.jsonSchema.type = 'string';
        ctx.jsonSchema.format = 'iso8601';
      }
    },
  };

  let bodySchema: undefined | ValidateFunction = undefined;
  if (spec.requestBody?.content && spec.requestBody.content['application/json']) {
    const zodSchema = Object.values(spec.requestBody.content)[0]!.schema! as z.ZodType;
    const jsonSchema = z.toJSONSchema(zodSchema, jsonSchemaOptions);
    jsonSchema.$id = jsonSchema.id;
    jsonSchema.id = undefined;
    for (const def in jsonSchema.$defs) {
      jsonSchema.$defs[def].$id = jsonSchema.$defs[def].id;
      jsonSchema.$defs[def].id = undefined;
    }
    bodySchema = ajv.compile(jsonSchema);
  }
  let querySchema: undefined | ValidateFunction = undefined;
  if (spec.requestParams?.query) {
    const zodSchema = spec.requestParams?.query as z.ZodType;
    const jsonSchema = z.toJSONSchema(zodSchema, jsonSchemaOptions);
    jsonSchema.$id = jsonSchema.id;
    jsonSchema.id = undefined;
    for (const def in jsonSchema.$defs) {
      jsonSchema.$defs[def].$id = jsonSchema.$defs[def].id;
      jsonSchema.$defs[def].id = undefined;
    }
    querySchema = ajv.compile(jsonSchema);
  }

  const pathSchema =
    spec.requestParams?.path && ajv.compile(z.toJSONSchema(spec.requestParams.path, jsonSchemaOptions));

  return function validateRequestBody(ctx: Context, next: Next) {
    if (bodySchema) {
      validate('body', bodySchema, ctx.request.body);
    }
    if (pathSchema) {
      validate('path', pathSchema, ctx.params);
    }
    if (querySchema) {
      validate('query', querySchema, ctx.query);
    }

    return next();
  };
};
